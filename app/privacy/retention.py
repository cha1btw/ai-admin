"""Retention (R9): conversations live retention_days (90 by default), raw inbound events 7 days.
Old bookings are kept without personal data so statistics still work."""

from datetime import timedelta

from sqlalchemy import and_, delete, exists, or_, select, update

from app.context import AppContext
from app.core.logging import get_logger
from app.db.models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    Client,
    InboundEvent,
    LinkToken,
    Message,
    OfferedSlot,
    Tenant,
)
from app.tenants.loader import studio_config

log = get_logger(__name__)

INBOUND_RETENTION = timedelta(days=7)


async def apply_retention(app: AppContext) -> dict[str, int]:
    now = app.clock.now()
    counts = {"messages": 0, "clients": 0, "bookings_anonymized": 0}
    async with app.session_factory() as session, session.begin():
        for tenant in list(await session.scalars(select(Tenant))):
            cutoff = now - timedelta(days=studio_config(tenant).retention_days)
            result = await session.execute(
                delete(Message).where(Message.tenant_id == tenant.id, Message.created_at < cutoff)
            )
            counts["messages"] += result.rowcount or 0
            result = await session.execute(
                update(Booking)
                .where(
                    Booking.tenant_id == tenant.id,
                    Booking.client_name.is_not(None) | Booking.client_phone.is_not(None),
                    or_(
                        Booking.end_at < cutoff,
                        and_(Booking.start_at.is_(None), Booking.created_at < cutoff),
                    ),
                )
                .values(client_name=None, client_phone=None)
            )
            counts["bookings_anonymized"] += result.rowcount or 0
            upcoming = exists().where(
                Booking.client_id == Client.id,
                Booking.status.in_(ACTIVE_BOOKING_STATUSES),
                or_(Booking.start_at.is_(None), Booking.start_at > now),
            )
            result = await session.execute(
                delete(Client).where(
                    Client.tenant_id == tenant.id,
                    or_(Client.last_seen_at < cutoff, Client.last_seen_at.is_(None)),
                    Client.created_at < cutoff,
                    ~upcoming,
                )
            )
            counts["clients"] += result.rowcount or 0
        await session.execute(
            delete(InboundEvent).where(InboundEvent.received_at < now - INBOUND_RETENTION)
        )
        await session.execute(
            delete(OfferedSlot).where(OfferedSlot.expires_at < now - timedelta(days=2))
        )
        await session.execute(
            delete(LinkToken).where(LinkToken.expires_at < now - timedelta(days=30))
        )
    log.info("retention.done", **counts)
    return counts
