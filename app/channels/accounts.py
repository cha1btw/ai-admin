from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChannelAccount


async def ensure_account(
    session: AsyncSession, tenant_id: int, kind: str, external_id: str, *, meta: dict | None = None
) -> ChannelAccount:
    account = await session.scalar(
        select(ChannelAccount).where(
            ChannelAccount.kind == kind, ChannelAccount.external_id == external_id
        )
    )
    if account is None:
        account = ChannelAccount(
            tenant_id=tenant_id,
            kind=kind,
            external_id=external_id,
            status="active",
            meta=meta or {},
        )
        session.add(account)
        await session.flush()
    return account
