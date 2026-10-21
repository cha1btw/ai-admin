"""Google Calendar provider (docs/research/google-calendar.md).

The master shares the calendar with our service account ("make changes to events"). Busy time
comes from freeBusy; all-day events count as closed days when google.all_day_events_block is on,
because Google shows them as "free" by default. Bookings are mirrored as events; our database
stays the record and Postgres prevents double bookings.
"""

from datetime import UTC, date, datetime, time
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.core.logging import get_logger
from app.db.models import Booking, Staff
from app.scheduling.base import CalendarError
from app.scheduling.google_auth import ServiceAccount, TokenSource
from app.scheduling.slots import Interval
from app.tenants.schema import StudioConfig

log = get_logger(__name__)

API = "https://www.googleapis.com/calendar/v3"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class GoogleCalendar:
    kind = "google"
    supports_slots = True

    def __init__(self, tokens: TokenSource, http: httpx.AsyncClient) -> None:
        self.tokens = tokens
        self.http = http

    @classmethod
    def from_file(cls, path: str) -> "GoogleCalendar":
        http = httpx.AsyncClient(timeout=10.0)
        return cls(TokenSource(ServiceAccount.from_file(path), http), http)

    @property
    def service_account_email(self) -> str:
        return self.tokens.account.client_email

    def _calendar_id(self, cfg: StudioConfig, staff: Staff) -> str:
        calendar_id = staff.google_calendar_id or cfg.google.calendar_id
        if not calendar_id:
            raise CalendarError(f"no Google calendar for staff {staff.key}")
        return calendar_id

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        token = await self.tokens.token()
        try:
            return await self.http.request(
                method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        except httpx.HTTPError as exc:
            raise CalendarError(f"{method} failed: {type(exc).__name__}") from exc

    async def busy(self, cfg: StudioConfig, staff: Staff, window: Interval) -> list[Interval]:
        calendar_id = self._calendar_id(cfg, staff)
        response = await self._request(
            "POST",
            f"{API}/freeBusy",
            json={
                "timeMin": _rfc3339(window.start),
                "timeMax": _rfc3339(window.end),
                "items": [{"id": calendar_id}],
            },
        )
        if response.status_code != 200:
            raise CalendarError(f"freeBusy returned {response.status_code}")
        calendar = response.json().get("calendars", {}).get(calendar_id, {})
        if calendar.get("errors"):
            reasons = [e.get("reason") for e in calendar["errors"]]
            raise CalendarError(f"freeBusy errors {reasons} (is the calendar shared?)")
        busy = [Interval(_parse(b["start"]), _parse(b["end"])) for b in calendar.get("busy", [])]
        if cfg.google.all_day_events_block:
            busy += await self._all_day_events(cfg, calendar_id, window)
        return busy

    async def _all_day_events(
        self, cfg: StudioConfig, calendar_id: str, window: Interval
    ) -> list[Interval]:
        response = await self._request(
            "GET",
            f"{API}/calendars/{quote(calendar_id, safe='')}/events",
            params={
                "timeMin": _rfc3339(window.start),
                "timeMax": _rfc3339(window.end),
                "singleEvents": "true",
                "maxResults": "250",
                "fields": "items(status,start,end)",
            },
        )
        if response.status_code != 200:
            raise CalendarError(f"events.list returned {response.status_code}")
        tz = ZoneInfo(cfg.timezone)
        closed = []
        for item in response.json().get("items", []):
            start_day = item.get("start", {}).get("date")
            if item.get("status") == "cancelled" or not start_day:
                continue
            end_day = item.get("end", {}).get("date") or start_day
            closed.append(
                Interval(
                    datetime.combine(date.fromisoformat(start_day), time(0), tzinfo=tz),
                    datetime.combine(date.fromisoformat(end_day), time(0), tzinfo=tz),
                )
            )
        return closed

    async def create_event(
        self, cfg: StudioConfig, staff: Staff, booking: Booking, title: str, description: str
    ) -> str | None:
        calendar_id = self._calendar_id(cfg, staff)
        response = await self._request(
            "POST",
            f"{API}/calendars/{quote(calendar_id, safe='')}/events",
            json={
                "summary": title,
                "description": description,
                "start": {"dateTime": booking.start_at.isoformat(), "timeZone": cfg.timezone},
                "end": {"dateTime": booking.end_at.isoformat(), "timeZone": cfg.timezone},
                "transparency": "opaque",
                "extendedProperties": {"private": {"ai_admin_booking": str(booking.id)}},
            },
        )
        if response.status_code not in (200, 201):
            raise CalendarError(f"events.insert returned {response.status_code}")
        return response.json()["id"]

    async def delete_event(self, cfg: StudioConfig, staff: Staff, event_id: str) -> None:
        calendar_id = self._calendar_id(cfg, staff)
        response = await self._request(
            "DELETE",
            f"{API}/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}",
        )
        if response.status_code in (200, 204, 404, 410):
            return  # 404/410: the master already deleted it
        raise CalendarError(f"events.delete returned {response.status_code}")

    async def event_exists(self, cfg: StudioConfig, staff: Staff, event_id: str) -> bool:
        """False if the master deleted our event in Google Calendar."""
        calendar_id = self._calendar_id(cfg, staff)
        response = await self._request(
            "GET",
            f"{API}/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}",
            params={"fields": "status"},
        )
        if response.status_code in (404, 410):
            return False
        if response.status_code != 200:
            raise CalendarError(f"events.get returned {response.status_code}")
        return response.json().get("status") != "cancelled"
