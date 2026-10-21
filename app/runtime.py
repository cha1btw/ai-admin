"""Wiring for long-running processes: bots, channels, calendars, notifier, model."""

from dataclasses import dataclass

from aiogram import Bot, Dispatcher

from app.channels.base import ChannelRegistry
from app.channels.cli import CLIChannel
from app.config import Settings
from app.context import AppContext
from app.core.alerts import Alerts
from app.core.clock import Clock, SystemClock
from app.core.logging import get_logger
from app.db.session import SessionFactory
from app.llm.base import LLMProvider
from app.llm.factory import LLMConfigError, UnavailableLLM, build_llm
from app.owner.notifier import OwnerNotifier, RecordingNotifier
from app.scheduling.registry import default_calendars

log = get_logger(__name__)

OWNER_UPDATES = [
    "message",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]
DEMO_UPDATES = ["message"]


@dataclass
class Bots:
    owner: Bot | None = None
    demo: Bot | None = None
    owner_dispatcher: Dispatcher | None = None
    demo_dispatcher: Dispatcher | None = None

    async def close(self) -> None:
        for bot in (self.owner, self.demo):
            if bot is not None:
                await bot.session.close()


def build_bots(settings: Settings) -> Bots:
    from app.channels.telegram_demo import build_demo_dispatcher
    from app.owner.bot import build_owner_dispatcher

    bots = Bots()
    if settings.owner_bot_token:
        bots.owner = Bot(token=settings.owner_bot_token.get_secret_value())
        bots.owner_dispatcher = build_owner_dispatcher()
    if settings.demo_bot_token:
        bots.demo = Bot(token=settings.demo_bot_token.get_secret_value())
        bots.demo_dispatcher = build_demo_dispatcher()
    return bots


def build_context(
    settings: Settings,
    session_factory: SessionFactory,
    bots: Bots,
    *,
    llm: LLMProvider | None = None,
    clock: Clock | None = None,
) -> AppContext:
    clock = clock or SystemClock()
    channels: ChannelRegistry = {"cli": CLIChannel()}
    notifier: OwnerNotifier
    if bots.owner is not None:
        from app.channels.telegram import SandboxChannel, TelegramBusinessChannel
        from app.owner.telegram import TelegramNotifier

        notifier = TelegramNotifier(bots.owner)
        channels["telegram_business"] = TelegramBusinessChannel(bots.owner)
        channels["sandbox"] = SandboxChannel(bots.owner)
    else:
        notifier = RecordingNotifier()
    if bots.demo is not None:
        from app.channels.telegram import TelegramDemoChannel

        channels["telegram_demo"] = TelegramDemoChannel(bots.demo)
    if settings.meta_app_secret is not None:
        from app.channels.instagram import InstagramChannel

        channels["instagram"] = InstagramChannel(settings)
    calendars = default_calendars()
    if settings.google_service_account_file:
        from app.scheduling.google import GoogleCalendar

        calendars["google"] = GoogleCalendar.from_file(settings.google_service_account_file)
    alerts = Alerts(notifier if bots.owner else None, settings.alert_chat_id, clock)
    if llm is None:
        try:
            llm = build_llm(settings)
        except LLMConfigError as exc:
            log.error("llm.not_configured", reason=str(exc))
            llm = UnavailableLLM(str(exc))
    return AppContext(
        settings=settings,
        session_factory=session_factory,
        llm=llm,
        clock=clock,
        channels=channels,
        calendars=calendars,
        notifier=notifier,
        alerts=alerts,
    )
