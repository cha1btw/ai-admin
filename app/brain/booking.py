"""Booking operations used by the model's tools and by Owner Bot.

Price and duration always come from the database at booking time (R3). A slot is booked only
from a token offered to this conversation, after a recheck under a per-master lock (R4); the
exclusion constraint in Postgres is the last line of defence.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain.phones import normalize_phone
from app.core.logging import get_logger
from app.db.models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    Client,
    Conversation,
    OfferedSlot,
    Service,
    Staff,
    Tenant,
)
from app.scheduling.availability import is_still_free
from app.scheduling.base import CalendarError, CalendarProvider
from app.tenants.schema import StudioConfig

log = get_logger(__name__)

STAFF_LOCK_SPACE = 2  # pg_advisory_xact_lock(2, staff_id)


class BookingError(Exception):
    """Expected business failure; `code` goes back to the model as data."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class BookingContext:
    session: AsyncSession
    tenant: Tenant
    cfg: StudioConfig
    provider: CalendarProvider
    now: datetime
    conversation: Conversation | None = None
    client: Client | None = None


@dataclass(frozen=True)
class BookingView:
    id: int | None
    status: str
    service_key: str
    service_name: str
    staff_name: str | None
    start: datetime | None  # studio local time
    price_type: str
    price_min: int | None
    price_max: int | None
    currency: str
    client_name: str | None
    client_phone: str | None
    prepayment_amount: int | None
    prepayment_due: datetime | None  # studio local time
    requested_time_text: str | None
    is_sandbox: bool = False


def clean_name(raw: str | None) -> str:
    name = " ".join((raw or "").split())[:60]
    if not any(ch.isalpha() for ch in name):
        raise BookingError("name_required")
    return name


def clean_phone(raw: str | None, *, required: bool) -> str | None:
    if not raw or not raw.strip():
        if required:
            raise BookingError("phone_required")
        return None
    phone = normalize_phone(raw)
    if phone is None:
        raise BookingError("phone_invalid")
    return phone


async def booking_view(session: AsyncSession, booking: Booking, cfg: StudioConfig) -> BookingView:
    service = await session.get(Service, booking.service_id)
    staff = await session.get(Staff, booking.staff_id) if booking.staff_id else None
    tz = ZoneInfo(cfg.timezone)
    return BookingView(
        id=booking.id,
        status=booking.status,
        service_key=service.key,
        service_name=service.name,
        staff_name=staff.name if staff else None,
        start=booking.start_at.astimezone(tz) if booking.start_at else None,
        price_type=booking.price_type,
        price_min=booking.price_min,
        price_max=booking.price_max,
        currency=booking.currency,
        client_name=booking.client_name,
        client_phone=booking.client_phone,
        prepayment_amount=booking.prepayment_amount,
        prepayment_due=booking.prepayment_due_at.astimezone(tz)
        if booking.prepayment_due_at
        else None,
        requested_time_text=booking.requested_time_text,
        is_sandbox=booking.is_sandbox,
    )


async def active_bookings(
    session: AsyncSession, client_id: int, now: datetime, *, sandbox: bool
) -> list[Booking]:
    rows = await session.scalars(
        select(Booking)
        .where(
            Booking.client_id == client_id,
            Booking.is_sandbox.is_(sandbox),
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            or_(Booking.start_at.is_(None), Booking.start_at > now),
        )
        .order_by(Booking.start_at.nulls_last(), Booking.id)
    )
    return list(rows)


async def _check_active_limit(ctx: BookingContext, *, replacing: int | None = None) -> None:
    current = [
        b
        for b in await active_bookings(
            ctx.session, ctx.client.id, ctx.now, sandbox=ctx.conversation.is_sandbox
        )
        if b.id != replacing
    ]
    if len(current) >= ctx.cfg.booking.max_active_bookings_per_client:
        raise BookingError("too_many_active")


async def create_booking(
    ctx: BookingContext,
    slot_token: str,
    client_name: str | None,
    client_phone: str | None,
    *,
    dry_run: bool = False,
    replacing: int | None = None,
) -> BookingView:
    session, conversation, client = ctx.session, ctx.conversation, ctx.client
    slot = await session.get(OfferedSlot, slot_token) if slot_token else None
    if slot is None or slot.conversation_id != conversation.id:
        raise BookingError("slot_unknown", "book only slot_id values returned by get_free_slots")
    if slot.expires_at <= ctx.now:
        raise BookingError("slot_expired", "call get_free_slots again")
    name = clean_name(client_name or client.name)
    phone = clean_phone(client_phone or client.phone, required=ctx.cfg.booking.require_phone)
    service = await session.get(Service, slot.service_id)
    staff = await session.get(Staff, slot.staff_id)
    service_cfg = ctx.cfg.service(service.key)
    if service_cfg is None or not service.active:
        raise BookingError("service_unknown")

    key = f"bk:{conversation.id}:{slot.token}"
    existing = await session.scalar(select(Booking).where(Booking.idempotency_key == key))
    if existing is not None:
        return await booking_view(session, existing, ctx.cfg)  # same call twice: same booking
    await _check_active_limit(ctx, replacing=replacing)

    sandbox = conversation.is_sandbox
    if not sandbox and not dry_run:
        await session.execute(select(func.pg_advisory_xact_lock(STAFF_LOCK_SPACE, staff.id)))
    free = await is_still_free(
        session, ctx.tenant.id, ctx.cfg, ctx.provider, service.key, staff, slot.start_at, ctx.now
    )
    if not free:
        raise BookingError("slot_taken", "offer other slots from get_free_slots")

    start = slot.start_at
    end = start + timedelta(minutes=service.duration_min)
    prepayment = ctx.cfg.prepayment_for(service_cfg)
    due = None
    if prepayment:
        due = min(
            ctx.now + timedelta(hours=ctx.cfg.prepayment.deadline_hours), start - timedelta(hours=1)
        )
        due = max(due, ctx.now + timedelta(minutes=30))
    booking = Booking(
        tenant_id=ctx.tenant.id,
        conversation_id=conversation.id,
        client_id=client.id,
        service_id=service.id,
        staff_id=staff.id,
        start_at=start,
        end_at=end,
        blocked_until=end + timedelta(minutes=service.buffer_min),
        status="pending_prepayment" if prepayment else "confirmed",
        price_type=service.price_type,
        price_min=service.price_min,
        price_max=service.price_max,
        currency=ctx.cfg.currency,
        client_name=name,
        client_phone=phone,
        created_by="bot",
        idempotency_key=key,
        is_sandbox=sandbox,
        prepayment_amount=prepayment,
        prepayment_due_at=due,
    )
    if dry_run:
        tz = ZoneInfo(ctx.cfg.timezone)
        return BookingView(
            id=None,
            status=booking.status,
            service_key=service.key,
            service_name=service.name,
            staff_name=staff.name,
            start=start.astimezone(tz),
            price_type=service.price_type,
            price_min=service.price_min,
            price_max=service.price_max,
            currency=ctx.cfg.currency,
            client_name=name,
            client_phone=phone,
            prepayment_amount=prepayment,
            prepayment_due=due.astimezone(tz) if due else None,
            requested_time_text=None,
            is_sandbox=sandbox,
        )

    try:
        async with session.begin_nested():
            session.add(booking)
            await session.flush()
            if not sandbox:
                title = f"{service.name}: {name}"
                description = f"Телефон: {phone or '—'}\nЗапис створив AI-адміністратор."
                booking.external_event_id = await ctx.provider.create_event(
                    ctx.cfg, staff, booking, title, description
                )
                await session.flush()
    except IntegrityError as exc:
        raise BookingError("slot_taken", "offer other slots from get_free_slots") from exc
    except CalendarError as exc:
        raise BookingError("calendar_unavailable", str(exc)) from exc

    client.name = name
    if phone:
        client.phone = phone
    log.info("booking.created", booking_id=booking.id, tenant=ctx.tenant.slug, sandbox=sandbox)
    return await booking_view(session, booking, ctx.cfg)


async def _owned_active(ctx: BookingContext, booking_id: int, *, by: str) -> Booking:
    booking = await ctx.session.get(Booking, booking_id, with_for_update=True)
    if booking is None or booking.tenant_id != ctx.tenant.id:
        raise BookingError("booking_not_found")
    if by == "client" and (ctx.client is None or booking.client_id != ctx.client.id):
        raise BookingError("booking_not_found")
    if booking.status not in ACTIVE_BOOKING_STATUSES:
        raise BookingError("not_active")
    if booking.start_at is not None and booking.start_at <= ctx.now:
        raise BookingError("in_past")
    return booking


def _late(ctx: BookingContext, booking: Booking) -> bool:
    if booking.start_at is None or booking.status == "requested":
        return False
    window = timedelta(hours=ctx.cfg.cancellation.free_cancel_hours)
    return booking.start_at - ctx.now < window


async def _delete_event(ctx: BookingContext, booking: Booking) -> None:
    if not booking.external_event_id or booking.staff_id is None or booking.is_sandbox:
        return
    staff = await ctx.session.get(Staff, booking.staff_id)
    try:
        await ctx.provider.delete_event(ctx.cfg, staff, booking.external_event_id)
    except CalendarError:
        # Our database is the record; the master is notified about the cancellation anyway.
        log.warning("calendar.delete_failed", booking_id=booking.id)


async def cancel_booking(
    ctx: BookingContext,
    booking_id: int,
    *,
    by: str = "client",
    reason: str | None = None,
    dry_run: bool = False,
) -> BookingView:
    booking = await _owned_active(ctx, booking_id, by=by)
    if by == "client" and _late(ctx, booking):
        raise BookingError("late_cancellation", "explain the studio rule and call handoff_to_human")
    view = await booking_view(ctx.session, booking, ctx.cfg)
    if dry_run:
        return view
    booking.status = "cancelled_by_client" if by == "client" else "cancelled_by_studio"
    booking.cancel_reason = reason
    await ctx.session.flush()
    await _delete_event(ctx, booking)
    return await booking_view(ctx.session, booking, ctx.cfg)


async def reschedule_booking(
    ctx: BookingContext, booking_id: int, slot_token: str, *, dry_run: bool = False
) -> tuple[BookingView, BookingView]:
    """Book the new slot first, then release the old one, atomically."""
    old = await _owned_active(ctx, booking_id, by="client")
    if _late(ctx, old):
        raise BookingError("late_cancellation", "explain the studio rule and call handoff_to_human")
    old_view = await booking_view(ctx.session, old, ctx.cfg)
    if dry_run:
        new_view = await create_booking(
            ctx, slot_token, old.client_name, old.client_phone, dry_run=True, replacing=old.id
        )
        return old_view, new_view
    async with ctx.session.begin_nested():
        # Release the old slot inside the same savepoint: if the new booking fails, it comes back.
        old.status = "cancelled_by_client"
        old.cancel_reason = "rescheduled"
        await ctx.session.flush()
        new_view = await create_booking(
            ctx, slot_token, old.client_name, old.client_phone, replacing=old.id
        )
    await _delete_event(ctx, old)
    return old_view, new_view


async def request_booking(
    ctx: BookingContext,
    service_key: str,
    preferred_time_text: str,
    client_name: str | None,
    client_phone: str | None,
    *,
    dry_run: bool = False,
) -> BookingView:
    """lead_capture: a request with a wished time; the master confirms the exact time."""
    session, conversation, client = ctx.session, ctx.conversation, ctx.client
    service = await session.scalar(
        select(Service).where(Service.tenant_id == ctx.tenant.id, Service.key == service_key)
    )
    if service is None or not service.active or ctx.cfg.service(service_key) is None:
        raise BookingError("service_unknown")
    name = clean_name(client_name or client.name)
    # The confirmation may come after the channel window closes, so the master needs a phone.
    phone = clean_phone(client_phone or client.phone, required=True)
    wish = " ".join((preferred_time_text or "").split())[:200]
    if not wish:
        raise BookingError("time_required")
    digest = hashlib.sha1(wish.lower().encode(), usedforsecurity=False).hexdigest()[:12]
    key = f"rq:{conversation.id}:{service.key}:{digest}"
    existing = await session.scalar(select(Booking).where(Booking.idempotency_key == key))
    if existing is not None:
        return await booking_view(session, existing, ctx.cfg)
    await _check_active_limit(ctx)
    booking = Booking(
        tenant_id=ctx.tenant.id,
        conversation_id=conversation.id,
        client_id=client.id,
        service_id=service.id,
        status="requested",
        requested_time_text=wish,
        price_type=service.price_type,
        price_min=service.price_min,
        price_max=service.price_max,
        currency=ctx.cfg.currency,
        client_name=name,
        client_phone=phone,
        created_by="bot",
        idempotency_key=key,
        is_sandbox=conversation.is_sandbox,
    )
    if dry_run:
        return BookingView(
            id=None,
            status="requested",
            service_key=service.key,
            service_name=service.name,
            staff_name=None,
            start=None,
            price_type=service.price_type,
            price_min=service.price_min,
            price_max=service.price_max,
            currency=ctx.cfg.currency,
            client_name=name,
            client_phone=phone,
            prepayment_amount=None,
            prepayment_due=None,
            requested_time_text=wish,
            is_sandbox=conversation.is_sandbox,
        )
    session.add(booking)
    await session.flush()
    client.name = name
    client.phone = phone
    return await booking_view(session, booking, ctx.cfg)


async def confirm_request(
    ctx: BookingContext, booking_id: int, start_local: datetime
) -> BookingView:
    """Owner Bot: the master confirmed a lead_capture request for an exact time."""
    booking = await _owned_active(ctx, booking_id, by="studio")
    if booking.status != "requested":
        raise BookingError("not_a_request")
    service = await ctx.session.get(Service, booking.service_id)
    start = start_local.astimezone(ZoneInfo(ctx.cfg.timezone))
    if start <= ctx.now:
        raise BookingError("in_past")
    booking.start_at = start
    booking.end_at = start + timedelta(minutes=service.duration_min)
    booking.blocked_until = booking.end_at + timedelta(minutes=service.buffer_min)
    booking.status = "confirmed"
    await ctx.session.flush()
    return await booking_view(ctx.session, booking, ctx.cfg)


async def reject_request(ctx: BookingContext, booking_id: int) -> BookingView:
    booking = await _owned_active(ctx, booking_id, by="studio")
    booking.status = "cancelled_by_studio"
    booking.cancel_reason = "rejected"
    await ctx.session.flush()
    return await booking_view(ctx.session, booking, ctx.cfg)


async def confirm_prepayment(ctx: BookingContext, booking_id: int) -> BookingView:
    booking = await _owned_active(ctx, booking_id, by="studio")
    if booking.status != "pending_prepayment":
        raise BookingError("not_pending_prepayment")
    booking.status = "confirmed"
    booking.prepayment_confirmed_at = ctx.now
    await ctx.session.flush()
    return await booking_view(ctx.session, booking, ctx.cfg)
