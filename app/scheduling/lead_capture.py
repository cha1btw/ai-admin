"""Lead capture: the bot cannot see free time (notebook, Altegio, Booksy...). It collects a
request and the master confirms the exact time in Owner Bot (request_booking tool)."""

from app.db.models import Booking, Staff
from app.scheduling.slots import Interval
from app.tenants.schema import StudioConfig


class LeadCaptureCalendar:
    kind = "lead_capture"
    supports_slots = False

    async def busy(self, cfg: StudioConfig, staff: Staff, window: Interval) -> list[Interval]:
        return []

    async def create_event(
        self, cfg: StudioConfig, staff: Staff, booking: Booking, title: str, description: str
    ) -> str | None:
        return None

    async def delete_event(self, cfg: StudioConfig, staff: Staff, event_id: str) -> None:
        return None
