"""Everything a request or a worker step needs, passed explicitly (easy to fake in tests)."""

from dataclasses import dataclass

from app.channels.base import ChannelRegistry
from app.config import Settings
from app.core.alerts import Alerts
from app.core.clock import Clock
from app.db.session import SessionFactory
from app.llm.base import LLMProvider
from app.owner.notifier import OwnerNotifier
from app.scheduling.base import CalendarRegistry


@dataclass
class AppContext:
    settings: Settings
    session_factory: SessionFactory
    llm: LLMProvider
    clock: Clock
    channels: ChannelRegistry
    calendars: CalendarRegistry
    notifier: OwnerNotifier
    alerts: Alerts
