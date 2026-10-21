"""Worker: inbound events, due replies and periodic jobs in one loop (`make worker`).

One worker is enough at our scale and keeps ordering simple: an Instagram echo of our reply is
processed after the reply's id is stored, so it is never mistaken for the master.
"""

import asyncio
import contextlib
import signal

from aiogram.types import Update
from sqlalchemy import Interval, func, or_, select

from app.brain.orchestrator import process_conversation
from app.config import get_settings
from app.context import AppContext
from app.core.logging import configure_logging, get_logger
from app.db.models import Conversation, InboundEvent
from app.db.session import make_engine, make_session_factory
from app.runtime import Bots, build_bots, build_context
from app.worker.jobs import Scheduler

log = get_logger(__name__)

MAX_ATTEMPTS = 5
RETRY_SECONDS = 30


def _backoff(attempts):
    """30 s after the first failure, 60 s after the second, and so on."""
    return func.make_interval(0, 0, 0, 0, 0, 0, attempts * RETRY_SECONDS, type_=Interval)


async def handle_event(app: AppContext, bots: Bots, event: InboundEvent) -> None:
    if event.source == "telegram_owner" and bots.owner is not None:
        update = Update.model_validate(event.payload, context={"bot": bots.owner})
        await bots.owner_dispatcher.feed_update(bots.owner, update, app=app)
    elif event.source == "telegram_demo" and bots.demo is not None:
        update = Update.model_validate(event.payload, context={"bot": bots.demo})
        await bots.demo_dispatcher.feed_update(bots.demo, update, app=app)
    elif event.source == "instagram":
        from app.channels.instagram import handle_instagram_event

        await handle_instagram_event(app, event.payload)
    elif event.source == "lead":
        from app.owner.notifications import notify_lead

        await notify_lead(app, int(event.payload["lead_id"]))
    else:
        raise RuntimeError(f"no handler for {event.source}")


async def process_inbound(app: AppContext, bots: Bots, limit: int = 20) -> int:
    now = app.clock.now()
    async with app.session_factory() as session:
        events = list(
            await session.scalars(
                select(InboundEvent)
                .where(
                    InboundEvent.status == "pending",
                    or_(
                        InboundEvent.attempts == 0,
                        InboundEvent.processed_at + _backoff(InboundEvent.attempts) < now,
                    ),
                )
                .order_by(InboundEvent.id)
                .limit(limit)
            )
        )
    for event in events:
        error = None
        try:
            await handle_event(app, bots, event)
        except Exception as exc:
            log.exception("inbound.failed", event_id=event.id, source=event.source)
            error = type(exc).__name__
        async with app.session_factory() as session, session.begin():
            row = await session.get(InboundEvent, event.id)
            row.attempts += 1
            row.processed_at = app.clock.now()
            row.error = error
            if error is None:
                row.status = "done"
            elif row.attempts >= MAX_ATTEMPTS:
                row.status = "failed"
                await app.alerts.send(
                    f"inbound_failed:{event.source}",
                    f"Подію {event.source} #{event.id} не вдалося обробити ({error}).",
                )
    return len(events)


async def process_due_replies(app: AppContext, limit: int = 10) -> int:
    async with app.session_factory() as session:
        ids = list(
            await session.scalars(
                select(Conversation.id)
                .where(Conversation.reply_due_at <= app.clock.now())
                .order_by(Conversation.reply_due_at)
                .limit(limit)
            )
        )
    for conversation_id in ids:
        await process_conversation(app, conversation_id)
    return len(ids)


async def run(app: AppContext, bots: Bots, stop: asyncio.Event) -> None:
    scheduler = Scheduler(app)
    log.info("worker.started", channels=sorted(app.channels))
    while not stop.is_set():
        try:
            worked = await process_inbound(app, bots) + await process_due_replies(app)
            await scheduler.tick()
        except Exception:
            log.exception("worker.loop_failed")
            await app.alerts.send("worker_loop", "Збій циклу воркера, перевірте логи.")
            worked = 0
        if not worked:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=app.settings.worker_poll_interval_s)
    log.info("worker.stopped")


async def amain() -> None:
    settings = get_settings()
    configure_logging()
    engine = make_engine(settings.database_url)
    bots = build_bots(settings)
    app = build_context(settings, make_session_factory(engine), bots)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run(app, bots, stop)
    finally:
        await bots.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(amain())
