"""Free slots from the database (bookings, blocks) plus the provider's own busy time."""

import secrets
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SLOT_HOLDING_STATUSES, Block, Booking, OfferedSlot, Service, Staff
from app.scheduling.base import CalendarProvider
from app.scheduling.slots import Interval, SlotOption, day_candidates, find_slots
from app.tenants.schema import StudioConfig

OFFER_TTL = timedelta(hours=24)


def day_window(cfg: StudioConfig, first: date, last: date) -> Interval:
    tz = ZoneInfo(cfg.timezone)
    start = datetime.combine(first, time(0), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
    return Interval(start, end)


async def staff_for_service(
    session: AsyncSession,
    tenant_id: int,
    cfg: StudioConfig,
    service_key: str,
    staff_key: str | None = None,
) -> list[Staff]:
    keys = [m.id for m in cfg.staff_for_service(service_key)]
    if staff_key is not None:
        keys = [k for k in keys if k == staff_key]
    if not keys:
        return []
    rows = await session.scalars(
        select(Staff).where(
            Staff.tenant_id == tenant_id, Staff.key.in_(keys), Staff.active.is_(True)
        )
    )
    by_key = {row.key: row for row in rows}
    return [by_key[k] for k in keys if k in by_key]


async def busy_by_staff(
    session: AsyncSession,
    tenant_id: int,
    staff_rows: list[Staff],
    window: Interval,
    provider: CalendarProvider,
    cfg: StudioConfig,
) -> dict[str, list[Interval]]:
    busy: dict[str, list[Interval]] = {s.key: [] for s in staff_rows}
    key_by_id = {s.id: s.key for s in staff_rows}
    rows = await session.execute(
        select(Booking.staff_id, Booking.start_at, Booking.blocked_until).where(
            Booking.tenant_id == tenant_id,
            Booking.staff_id.in_(list(key_by_id)),
            Booking.status.in_(SLOT_HOLDING_STATUSES),
            Booking.is_sandbox.is_(False),
            Booking.start_at < window.end,
            Booking.blocked_until > window.start,
        )
    )
    for staff_id, start, until in rows:
        busy[key_by_id[staff_id]].append(Interval(start, until))

    blocks = await session.scalars(
        select(Block).where(
            Block.tenant_id == tenant_id, Block.start_at < window.end, Block.end_at > window.start
        )
    )
    for block in blocks:
        targets = list(busy) if block.staff_id is None else [key_by_id.get(block.staff_id)]
        for key in targets:
            if key is not None:
                busy[key].append(Interval(block.start_at, block.end_at))

    for staff in staff_rows:
        busy[staff.key].extend(await provider.busy(cfg, staff, window))
    return busy


async def free_slots(
    session: AsyncSession,
    tenant_id: int,
    cfg: StudioConfig,
    provider: CalendarProvider,
    service_key: str,
    date_from: date,
    date_to: date,
    now: datetime,
    *,
    staff_key: str | None = None,
    time_from: time | None = None,
    time_to: time | None = None,
    limit: int | None = None,
) -> list[SlotOption]:
    staff_rows = await staff_for_service(session, tenant_id, cfg, service_key, staff_key)
    if not staff_rows or date_to < date_from:
        return []
    busy = await busy_by_staff(
        session, tenant_id, staff_rows, day_window(cfg, date_from, date_to), provider, cfg
    )
    return find_slots(
        cfg,
        service_key,
        [s.key for s in staff_rows],
        date_from,
        date_to,
        now,
        busy,
        time_from=time_from,
        time_to=time_to,
        limit=limit,
    )


async def is_still_free(
    session: AsyncSession,
    tenant_id: int,
    cfg: StudioConfig,
    provider: CalendarProvider,
    service_key: str,
    staff: Staff,
    start: datetime,
    now: datetime,
) -> bool:
    """Recheck a slot against current data: hours, breaks, lead time, bookings, blocks,
    the external calendar. Called inside the booking transaction (R4)."""
    service = cfg.service(service_key)
    if service is None:
        return False
    day = start.astimezone(ZoneInfo(cfg.timezone)).date()
    busy = await busy_by_staff(
        session, tenant_id, [staff], day_window(cfg, day, day), provider, cfg
    )
    earliest = now + timedelta(minutes=cfg.booking.min_lead_minutes)
    candidates = day_candidates(cfg, service, day, busy[staff.key], earliest)
    return any(c.start == start for c in candidates)


async def offer_slots(
    session: AsyncSession,
    conversation_id: int,
    service: Service,
    staff_by_key: dict[str, Staff],
    options: list[SlotOption],
    now: datetime,
) -> list[OfferedSlot]:
    """Persist slot tokens: the model can only book what was offered here (R4)."""
    offered = []
    for option in options:
        slot = OfferedSlot(
            token="s_" + secrets.token_urlsafe(6),
            conversation_id=conversation_id,
            service_id=service.id,
            staff_id=staff_by_key[option.staff_key].id,
            start_at=option.start,
            end_at=option.end,
            created_at=now,
            expires_at=now + OFFER_TTL,
        )
        session.add(slot)
        offered.append(slot)
    await session.flush()
    return offered
