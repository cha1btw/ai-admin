"""Storing raw inbound events: webhooks and the development poller both end here (R9: 7 days)."""

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InboundEvent


async def store_event(
    session: AsyncSession, source: str, external_id: str, payload: dict[str, Any]
) -> bool:
    """Insert once; a repeated delivery (webhook retry) is ignored. Returns True if new."""
    statement = (
        insert(InboundEvent)
        .values(
            source=source, external_id=external_id, payload=payload, status="pending", attempts=0
        )
        .on_conflict_do_nothing(index_elements=["source", "external_id"])
    )
    result = await session.execute(statement)
    return bool(result.rowcount)
