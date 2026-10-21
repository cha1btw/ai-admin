"""Google Calendar provider against a fake Google API (httpx.MockTransport)."""

import base64
import json
from datetime import date

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import func, select

from app.brain import booking as bookings
from app.brain.orchestrator import process_conversation
from app.db.models import Booking, Service, Tenant
from app.llm.fake import call
from app.scheduling.availability import free_slots, offer_slots, staff_for_service
from app.scheduling.base import CalendarError
from app.scheduling.google import GoogleCalendar
from app.scheduling.google_auth import ServiceAccount, TokenSource, make_assertion
from app.tenants.loader import studio_config
from app.tenants.schema import StudioConfig
from app.worker.jobs import reconcile_google_bookings
from tests.helpers import KYIV, demo_config_dict
from tests.integration.conftest import client_says, make_studio

CALENDAR = "studio@group.calendar.google.com"


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def account(key) -> ServiceAccount:
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return ServiceAccount("bot@project.iam.gserviceaccount.com", pem)


class FakeGoogle:
    def __init__(self, public_key) -> None:
        self.public_key = public_key
        self.busy: list[dict] = []
        self.all_day: list[dict] = []
        self.events: dict[str, dict] = {}
        self.fail_insert = False
        self.token_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.token_requests += 1
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            header, claims, signature = form["assertion"].split(".")
            self.public_key.verify(
                _b64decode(signature),
                f"{header}.{claims}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        path, method = request.url.path, request.method
        if path.endswith("/freeBusy"):
            calendar = json.loads(request.content)["items"][0]["id"]
            return httpx.Response(200, json={"calendars": {calendar: {"busy": self.busy}}})
        if method == "GET" and path.endswith("/events"):
            return httpx.Response(200, json={"items": self.all_day})
        if method == "POST" and path.endswith("/events"):
            if self.fail_insert:
                return httpx.Response(500, json={})
            event_id = f"ev{len(self.events) + 1}"
            self.events[event_id] = json.loads(request.content)
            return httpx.Response(200, json={"id": event_id})
        event_id = path.rsplit("/", 1)[-1]
        if method == "DELETE":
            return httpx.Response(204 if self.events.pop(event_id, None) else 410)
        if method == "GET":
            if event_id in self.events:
                return httpx.Response(200, json={"status": "confirmed"})
            return httpx.Response(404, json={})
        return httpx.Response(400)


@pytest.fixture
def google(account, key):
    fake = FakeGoogle(key.public_key())
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return GoogleCalendar(TokenSource(account, http), http), fake


def test_assertion_is_signed_rs256_with_required_claims(account, key) -> None:
    assertion = make_assertion(account, "https://www.googleapis.com/auth/calendar", now=1000)
    header, claims, signature = assertion.split(".")
    key.public_key().verify(
        _b64decode(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    assert json.loads(_b64decode(header)) == {"alg": "RS256", "typ": "JWT"}
    payload = json.loads(_b64decode(claims))
    assert payload["iss"] == account.client_email
    assert payload["aud"] == "https://oauth2.googleapis.com/token"
    assert payload["exp"] - payload["iat"] == 3600


async def test_token_is_cached(google) -> None:
    provider, fake = google
    await provider.tokens.token()
    await provider.tokens.token()
    assert fake.token_requests == 1


async def _google_studio(app, provider) -> object:
    data = demo_config_dict()
    data["calendar_provider"] = "google"
    data["google"] = {"calendar_id": CALENDAR, "all_day_events_block": True}
    app.calendars["google"] = provider
    return await make_studio(app, cfg=StudioConfig.model_validate(data), kind="telegram_business")


async def test_busy_time_and_all_day_events_close_slots(app, google, clock) -> None:
    provider, fake = google
    studio = await _google_studio(app, provider)
    fake.busy = [
        {"start": "2026-10-21T07:00:00Z", "end": "2026-10-21T13:00:00Z"}
    ]  # 10:00-16:00 Kyiv
    fake.all_day = [
        {"status": "confirmed", "start": {"date": "2026-10-22"}, "end": {"date": "2026-10-23"}}
    ]
    async with app.session_factory() as session:
        tenant = await session.get(Tenant, studio.tenant_id)
        cfg = studio_config(tenant)
        wednesday = await free_slots(
            session,
            tenant.id,
            cfg,
            provider,
            "gel_manicure",
            date(2026, 10, 21),
            date(2026, 10, 21),
            clock.now(),
            limit=20,
        )
        thursday = await free_slots(
            session,
            tenant.id,
            cfg,
            provider,
            "gel_manicure",
            date(2026, 10, 22),
            date(2026, 10, 22),
            clock.now(),
            limit=20,
        )
    assert wednesday and all(s.start.astimezone(KYIV).hour >= 16 for s in wednesday)
    assert thursday == []  # the all-day event closes the day


async def test_booking_creates_event_and_cancel_deletes_it(app, google, clock) -> None:
    provider, fake = google
    studio = await _google_studio(app, provider)
    async with app.session_factory() as session, session.begin():
        tenant = await session.get(Tenant, studio.tenant_id)
        cfg = studio_config(tenant)
        from tests.integration.test_booking import _conversation

        client, conversation = await _conversation(session, studio, "anna")
        options = await free_slots(
            session,
            tenant.id,
            cfg,
            provider,
            "gel_manicure",
            date(2026, 10, 23),
            date(2026, 10, 23),
            clock.now(),
        )
        service = await session.scalar(select(Service).where(Service.key == "gel_manicure"))
        staff = {s.key: s for s in await staff_for_service(session, tenant.id, cfg, "gel_manicure")}
        offered = await offer_slots(
            session, conversation.id, service, staff, options[:1], clock.now()
        )
        ctx = bookings.BookingContext(
            session, tenant, cfg, provider, clock.now(), conversation, client
        )
        view = await bookings.create_booking(ctx, offered[0].token, "Анна", None)
        booking = await session.get(Booking, view.id)
        event = fake.events[booking.external_event_id]
        assert event["start"]["dateTime"] == booking.start_at.isoformat()
        assert event["extendedProperties"]["private"]["ai_admin_booking"] == str(booking.id)
        assert event["summary"] == "Манікюр з гель-лаком: Анна"
        await bookings.cancel_booking(ctx, view.id, by="studio")
    assert fake.events == {}


async def test_calendar_failure_rolls_back_booking(app, google, clock) -> None:
    provider, fake = google
    studio = await _google_studio(app, provider)
    fake.fail_insert = True
    async with app.session_factory() as session, session.begin():
        tenant = await session.get(Tenant, studio.tenant_id)
        cfg = studio_config(tenant)
        from tests.integration.test_booking import _conversation

        client, conversation = await _conversation(session, studio, "anna")
        options = await free_slots(
            session,
            tenant.id,
            cfg,
            provider,
            "gel_manicure",
            date(2026, 10, 23),
            date(2026, 10, 23),
            clock.now(),
        )
        service = await session.scalar(select(Service).where(Service.key == "gel_manicure"))
        staff = {s.key: s for s in await staff_for_service(session, tenant.id, cfg, "gel_manicure")}
        offered = await offer_slots(
            session, conversation.id, service, staff, options[:1], clock.now()
        )
        ctx = bookings.BookingContext(
            session, tenant, cfg, provider, clock.now(), conversation, client
        )
        with pytest.raises(bookings.BookingError, match="calendar_unavailable"):
            await bookings.create_booking(ctx, offered[0].token, "Анна", None)
        assert await session.scalar(select(func.count()).select_from(Booking)) == 0


async def test_unshared_calendar_reaches_the_model_as_data(app, google, llm, clock) -> None:
    provider, fake = google

    def unshared(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/freeBusy"):
            return httpx.Response(
                200, json={"calendars": {CALENDAR: {"errors": [{"reason": "notFound"}]}}}
            )
        return fake(request)

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(unshared))
    provider.tokens._http = provider.http
    studio = await _google_studio(app, provider)
    llm.add(
        call(
            "get_free_slots",
            service_id="gel_manicure",
            date_from="2026-10-21",
            date_to="2026-10-21",
        ),
        call("handoff_to_human", reason="other", summary="Календар недоступний"),
    )
    cid = await client_says(app, studio, "Є вікно завтра?")
    await process_conversation(app, cid)
    tool_result = json.loads(llm.calls[1][0][-1].content)
    assert tool_result["error"] == "calendar_unavailable"
    with pytest.raises(CalendarError, match="notFound"):
        async with app.session_factory() as session:
            tenant = await session.get(Tenant, studio.tenant_id)
            staff = (
                await staff_for_service(session, tenant.id, studio_config(tenant), "gel_manicure")
            )[0]
            from app.scheduling.slots import Interval

            await provider.busy(studio_config(tenant), staff, Interval(clock.now(), clock.now()))


async def test_event_deleted_by_master_cancels_booking(app, google, clock) -> None:
    provider, fake = google
    studio = await _google_studio(app, provider)
    async with app.session_factory() as session, session.begin():
        tenant = await session.get(Tenant, studio.tenant_id)
        cfg = studio_config(tenant)
        from tests.integration.test_booking import _conversation

        client, conversation = await _conversation(session, studio, "anna")
        options = await free_slots(
            session,
            tenant.id,
            cfg,
            provider,
            "gel_manicure",
            date(2026, 10, 23),
            date(2026, 10, 23),
            clock.now(),
        )
        service = await session.scalar(select(Service).where(Service.key == "gel_manicure"))
        staff = {s.key: s for s in await staff_for_service(session, tenant.id, cfg, "gel_manicure")}
        offered = await offer_slots(
            session, conversation.id, service, staff, options[:1], clock.now()
        )
        ctx = bookings.BookingContext(
            session, tenant, cfg, provider, clock.now(), conversation, client
        )
        view = await bookings.create_booking(ctx, offered[0].token, "Анна", "0671234567")
    fake.events.clear()  # the master deleted it in Google Calendar
    await reconcile_google_bookings(app)
    async with app.session_factory() as session:
        booking = await session.get(Booking, view.id)
    assert (booking.status, booking.cancel_reason) == ("cancelled_by_studio", "deleted_in_calendar")
    assert any(n.text.startswith("🗑 Подію Анна") for n in app.notifier.sent)
