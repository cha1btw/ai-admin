import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.brain import booking as bookings
from app.brain.booking import BookingContext, BookingError
from app.brain.ingest import get_or_create_conversation
from app.db.models import Booking, ChannelAccount, Client, Conversation, Service, Staff, Tenant
from app.scheduling.availability import free_slots, offer_slots, staff_for_service
from app.scheduling.internal import InternalCalendar
from app.tenants.loader import load_tenant, studio_config
from tests.helpers import demo_config_dict, kyiv
from tests.integration.conftest import make_studio

NOW = kyiv("2026-10-20 23:47")
PROVIDER = InternalCalendar()


async def _conversation(session, studio, user: str, *, sandbox: bool = False):
    account = await session.get(ChannelAccount, studio.account_id)
    return await get_or_create_conversation(session, account, user, NOW, sandbox=sandbox)


async def _ctx(session, studio, user: str, *, now=NOW, sandbox: bool = False) -> BookingContext:
    tenant = await session.get(Tenant, studio.tenant_id)
    client, conversation = await _conversation(session, studio, user, sandbox=sandbox)
    return BookingContext(
        session=session,
        tenant=tenant,
        cfg=studio_config(tenant),
        provider=PROVIDER,
        now=now,
        conversation=conversation,
        client=client,
    )


async def _offer(session, ctx: BookingContext, day: date, staff_key: str = "olena", n: int = 6):
    options = await free_slots(
        session,
        ctx.tenant.id,
        ctx.cfg,
        PROVIDER,
        "gel_manicure",
        day,
        day,
        ctx.now,
        staff_key=staff_key,
    )
    service = await session.scalar(select(Service).where(Service.key == "gel_manicure"))
    staff = {
        s.key: s for s in await staff_for_service(session, ctx.tenant.id, ctx.cfg, "gel_manicure")
    }
    return await offer_slots(session, ctx.conversation.id, service, staff, options[:n], ctx.now)


async def test_booking_uses_database_price_at_booking_time(app) -> None:
    studio = await make_studio(app)
    async with app.session_factory() as session, session.begin():
        ctx = await _ctx(session, studio, "anna")
        slot = (await _offer(session, ctx, date(2026, 10, 21)))[0]
        token = slot.token
    # The studio raises the price after the slot was offered.
    data = demo_config_dict()
    data["services"][0]["price_min"] = 950
    async with app.session_factory() as session, session.begin():
        from app.tenants.schema import StudioConfig

        await load_tenant(session, StudioConfig.model_validate(data))
    async with app.session_factory() as session, session.begin():
        ctx = await _ctx(session, studio, "anna")
        view = await bookings.create_booking(ctx, token, "Анна", None)
    assert view.price_min == 950 and view.status == "confirmed"
    assert view.staff_name == "Олена"


async def test_same_call_twice_returns_the_same_booking(app) -> None:
    studio = await make_studio(app)
    async with app.session_factory() as session, session.begin():
        ctx = await _ctx(session, studio, "anna")
        token = (await _offer(session, ctx, date(2026, 10, 21)))[0].token
        first = await bookings.create_booking(ctx, token, "Анна", None)
        second = await bookings.create_booking(ctx, token, "Анна", None)
        count = await session.scalar(select(func.count()).select_from(Booking))
    assert first.id == second.id and count == 1


async def test_two_clients_race_for_one_slot(app) -> None:
    """R4: concurrent bookings of the same master and time: exactly one wins."""
    studio = await make_studio(app)
    tokens = {}
    async with app.session_factory() as session, session.begin():
        for user in ("anna", "olha"):
            ctx = await _ctx(session, studio, user)
            tokens[user] = (await _offer(session, ctx, date(2026, 10, 21), n=1))[0].token

    async def attempt(user: str):
        async with app.session_factory() as session, session.begin():
            ctx = await _ctx(session, studio, user)
            try:
                return await bookings.create_booking(ctx, tokens[user], user.title(), None)
            except BookingError as exc:
                return exc.code

    results = await asyncio.gather(attempt("anna"), attempt("olha"))
    assert sorted(isinstance(r, str) for r in results) == [False, True]
    assert "slot_taken" in results
    async with app.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Booking)) == 1


async def test_tokens_are_bound_to_conversation_and_expire(app) -> None:
    studio = await make_studio(app)
    async with app.session_factory() as session, session.begin():
        anna = await _ctx(session, studio, "anna")
        token = (await _offer(session, anna, date(2026, 10, 21)))[0].token
        olha = await _ctx(session, studio, "olha")
        with pytest.raises(BookingError, match="slot_unknown"):
            await bookings.create_booking(olha, token, "Ольга", None)
        with pytest.raises(BookingError, match="slot_unknown"):
            await bookings.create_booking(anna, "s_invented", "Анна", None)
        later = await _ctx(session, studio, "anna", now=NOW + timedelta(hours=25))
        with pytest.raises(BookingError, match="slot_expired"):
            await bookings.create_booking(later, token, "Анна", None)


async def test_phone_rules(app) -> None:
    data = demo_config_dict()
    data["booking"]["require_phone"] = True
    from app.tenants.schema import StudioConfig

    studio = await make_studio(app, cfg=StudioConfig.model_validate(data))
    async with app.session_factory() as session, session.begin():
        ctx = await _ctx(session, studio, "anna")
        token = (await _offer(session, ctx, date(2026, 10, 21)))[0].token
        with pytest.raises(BookingError, match="phone_required"):
            await bookings.create_booking(ctx, token, "Анна", None)
        with pytest.raises(BookingError, match="phone_invalid"):
            await bookings.create_booking(ctx, token, "Анна", "123")
        view = await bookings.create_booking(ctx, token, "Анна", "067 123 45 67")
        assert view.client_phone == "+380671234567"
        client = await session.get(Client, ctx.client.id)
        assert client.phone == "+380671234567"  # remembered for the next booking


async def _offer_at(session, ctx: BookingContext, start_local):
    """Offer exactly one slot: the given local start time with Olena."""
    at = start_local.time()
    options = await free_slots(
        session,
        ctx.tenant.id,
        ctx.cfg,
        PROVIDER,
        "gel_manicure",
        start_local.date(),
        start_local.date(),
        ctx.now,
        staff_key="olena",
        time_from=at,
        time_to=at,
    )
    service = await session.scalar(select(Service).where(Service.key == "gel_manicure"))
    staff = {
        s.key: s for s in await staff_for_service(session, ctx.tenant.id, ctx.cfg, "gel_manicure")
    }
    offered = await offer_slots(session, ctx.conversation.id, service, staff, options, ctx.now)
    assert len(offered) == 1
    return offered[0].token


async def test_late_cancellation_needs_a_human(app) -> None:
    studio = await make_studio(app)
    async with app.session_factory() as session, session.begin():
        ctx = await _ctx(session, studio, "anna")
        token = await _offer_at(session, ctx, kyiv("2026-10-21 10:00"))
        view = await bookings.create_booking(ctx, token, "Анна", None)
        # The visit is about 10 hours away: within the 24-hour rule.
        with pytest.raises(BookingError, match="late_cancellation"):
            await bookings.cancel_booking(ctx, view.id)
        # The studio itself may cancel any time.
        cancelled = await bookings.cancel_booking(ctx, view.id, by="studio", reason="sick")
        assert cancelled.status == "cancelled_by_studio"


async def test_reschedule_is_atomic(app) -> None:
    studio = await make_studio(app)
    async with app.session_factory() as session, session.begin():
        anna = await _ctx(session, studio, "anna")
        first = await _offer_at(session, anna, kyiv("2026-10-23 10:00"))
        booking = await bookings.create_booking(anna, first, "Анна", None)
        wanted = await _offer_at(session, anna, kyiv("2026-10-23 14:00"))
        # Before Anna moves, Olha takes 14:00 with the same master.
        olha = await _ctx(session, studio, "olha")
        await bookings.create_booking(
            olha, await _offer_at(session, olha, kyiv("2026-10-23 14:00")), "Ольга", None
        )
        with pytest.raises(BookingError, match="slot_taken"):
            await bookings.reschedule_booking(anna, booking.id, wanted)
        old = await session.get(Booking, booking.id)
        assert old.status == "confirmed"  # the old booking survived the failed move

        free = await _offer_at(session, anna, kyiv("2026-10-23 17:00"))
        _, new_view = await bookings.reschedule_booking(anna, booking.id, free)
        assert new_view.start == kyiv("2026-10-23 17:00")
        assert (await session.get(Booking, booking.id)).status == "cancelled_by_client"


async def test_free_cancellation_releases_slot(app) -> None:
    studio = await make_studio(app)
    async with app.session_factory() as session, session.begin():
        ctx = await _ctx(session, studio, "anna")
        token = (await _offer(session, ctx, date(2026, 10, 23)))[0].token
        view = await bookings.create_booking(ctx, token, "Анна", None)
        cancelled = await bookings.cancel_booking(ctx, view.id)
        assert cancelled.status == "cancelled_by_client"
        again = await _offer(session, ctx, date(2026, 10, 23))
        assert again[0].start_at.astimezone(kyiv("2026-10-23 00:00").tzinfo) == view.start


async def test_active_booking_limit(app) -> None:
    studio = await make_studio(app)  # demo: max 2 active bookings per client
    async with app.session_factory() as session, session.begin():
        ctx = await _ctx(session, studio, "anna")
        for day in (22, 23):
            token = (await _offer(session, ctx, date(2026, 10, day)))[0].token
            await bookings.create_booking(ctx, token, "Анна", None)
        token = (await _offer(session, ctx, date(2026, 10, 26)))[0].token
        with pytest.raises(BookingError, match="too_many_active"):
            await bookings.create_booking(ctx, token, "Анна", None)


async def test_sandbox_bookings_do_not_block_real_clients(app) -> None:
    studio = await make_studio(app)
    async with app.session_factory() as session, session.begin():
        master = await _ctx(session, studio, "owner-sandbox", sandbox=True)
        token = (await _offer(session, master, date(2026, 10, 21), n=1))[0].token
        view = await bookings.create_booking(master, token, "Тест", None)
        assert view.is_sandbox
        client = await _ctx(session, studio, "anna")
        offered = await _offer(session, client, date(2026, 10, 21), n=1)
        assert offered[0].start_at.astimezone(view.start.tzinfo) == view.start


async def test_lead_capture_request_needs_phone(app) -> None:
    data = demo_config_dict()
    data["calendar_provider"] = "lead_capture"
    from app.tenants.schema import StudioConfig

    studio = await make_studio(app, cfg=StudioConfig.model_validate(data))
    async with app.session_factory() as session, session.begin():
        ctx = await _ctx(session, studio, "anna")
        with pytest.raises(BookingError, match="phone_required"):
            await bookings.request_booking(
                ctx, "gel_manicure", "п'ятниця після 15:00", "Анна", None
            )
        view = await bookings.request_booking(
            ctx, "gel_manicure", "п'ятниця, 23.10, після 15:00", "Анна", "0671234567"
        )
        assert view.status == "requested" and view.start is None and view.price_min == 900
        confirmed = await bookings.confirm_request(ctx, view.id, kyiv("2026-10-23 15:30"))
        assert confirmed.status == "confirmed"
        assert confirmed.start == kyiv("2026-10-23 15:30")
        staff_count = await session.scalar(select(func.count()).select_from(Staff))
        assert staff_count == 2
        conversation = await session.get(Conversation, ctx.conversation.id)
        assert conversation is not None
