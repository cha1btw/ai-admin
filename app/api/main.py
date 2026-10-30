"""HTTP entry point: health check, webhooks, landing form and the admin panel.

Webhooks only store events (fast 200); the worker does the rest (see docs/PRODUCT.md,
section 1). The admin panel's test chat runs the brain inline, so the API has its own AppContext.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.admin.api import STATIC
from app.admin.api import router as admin_router
from app.admin.service import TEST_CHANNEL, CaptureChannel
from app.api import health, instagram, leads, telegram
from app.config import Settings, get_settings
from app.core.clock import Clock
from app.core.heartbeat import WorkerWatchdog
from app.core.logging import configure_logging, get_logger
from app.db.session import make_engine, make_session_factory
from app.llm.base import LLMProvider
from app.runtime import Bots, build_context

log = get_logger(__name__)
WATCHDOG_EVERY_S = 30


class _Watchdog:
    def __init__(self, task: asyncio.Task, bot) -> None:
        self._task, self._bot = task, bot

    async def stop(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        await self._bot.session.close()


async def _start_watchdog(settings: Settings, factory) -> _Watchdog | None:
    """The API outlives a crashed worker, so it watches the worker's pulse and alerts founders.
    Needs the Owner Bot token and ALERT_CHAT_ID; without them only /health reports it."""
    if not settings.owner_bot_token or settings.alert_chat_id is None:
        return None
    from aiogram import Bot

    from app.owner.telegram import TelegramNotifier

    bot = Bot(token=settings.owner_bot_token.get_secret_value())
    notifier = TelegramNotifier(bot)
    chat_id = settings.alert_chat_id

    async def notify(text: str) -> None:
        await notifier.send(chat_id, f"⚠️ {text}")

    watchdog = WorkerWatchdog(factory, notify)

    async def loop() -> None:
        while True:
            try:
                await watchdog.check()
            except Exception:
                log.exception("watchdog.failed")
            await asyncio.sleep(WATCHDOG_EVERY_S)

    return _Watchdog(asyncio.create_task(loop()), bot)


def create_app(
    settings: Settings | None = None, *, llm: LLMProvider | None = None, clock: Clock | None = None
) -> FastAPI:
    """llm and clock let tests drive the admin test chat with FakeLLM and a frozen clock."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        engine = make_engine(settings.database_url)
        app.state.settings = settings
        app.state.session_factory = make_session_factory(engine)
        app.state.lead_limiter = leads.RateLimiter()
        # No bots here: sandbox conversations never notify anyone, replies are captured.
        admin = build_context(settings, app.state.session_factory, Bots(), llm=llm, clock=clock)
        admin.channels[TEST_CHANNEL] = CaptureChannel()
        app.state.admin = admin
        watchdog = await _start_watchdog(settings, app.state.session_factory)
        yield
        if watchdog is not None:
            await watchdog.stop()
        await engine.dispose()

    app = FastAPI(title="AI-admin", lifespan=lifespan, docs_url=None, redoc_url=None)
    if settings.landing_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.landing_origin.rstrip("/")],
            allow_methods=["POST"],
            allow_headers=["Content-Type"],
        )
    app.include_router(health.router)
    app.include_router(telegram.router)
    app.include_router(instagram.router)
    app.include_router(leads.router)
    app.include_router(admin_router)
    app.mount("/admin/static", StaticFiles(directory=STATIC), name="admin-static")
    return app


app = create_app()
