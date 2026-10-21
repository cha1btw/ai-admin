"""Development only: fetch Telegram updates by long polling and store them like webhooks do.

Run together with `make worker`. It removes the bots' webhooks, so do not run it against the
production bots.
"""

import asyncio

from aiogram import Bot

from app.config import get_settings
from app.core.inbox import store_event
from app.core.logging import configure_logging, get_logger
from app.db.session import SessionFactory, make_engine, make_session_factory
from app.runtime import DEMO_UPDATES, OWNER_UPDATES, build_bots

log = get_logger(__name__)


async def poll_bot(factory: SessionFactory, bot: Bot, source: str, allowed: list[str]) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    offset: int | None = None
    log.info("poll.started", source=source)
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=25, allowed_updates=allowed)
        except Exception:
            log.exception("poll.failed", source=source)
            await asyncio.sleep(5)
            continue
        if not updates:
            continue
        async with factory() as session, session.begin():
            for update in updates:
                payload = update.model_dump(mode="json", exclude_none=True, by_alias=True)
                await store_event(session, source, str(update.update_id), payload)
        offset = updates[-1].update_id + 1


async def amain() -> None:
    settings = get_settings()
    configure_logging()
    factory = make_session_factory(make_engine(settings.database_url))
    bots = build_bots(settings)
    tasks = []
    if bots.owner is not None:
        tasks.append(poll_bot(factory, bots.owner, "telegram_owner", OWNER_UPDATES))
    if bots.demo is not None:
        tasks.append(poll_bot(factory, bots.demo, "telegram_demo", DEMO_UPDATES))
    if not tasks:
        raise SystemExit("Set OWNER_BOT_TOKEN and/or DEMO_BOT_TOKEN in .env")
    try:
        await asyncio.gather(*tasks)
    finally:
        await bots.close()


if __name__ == "__main__":
    asyncio.run(amain())
