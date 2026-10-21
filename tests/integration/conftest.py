"""Fixtures for pipeline tests: a frozen clock, a scripted model, recording channels."""

import json
import re
from dataclasses import dataclass
from datetime import timedelta

import pytest

from app.brain.ingest import Inbound, record_client_message
from app.channels.accounts import ensure_account
from app.channels.cli import CLIChannel
from app.config import Settings
from app.context import AppContext
from app.core.alerts import Alerts
from app.core.clock import FrozenClock
from app.db.models import ChannelAccount, OwnerAccount, Tenant, TenantMember
from app.db.session import SessionFactory
from app.llm.base import ChatMessage, LLMResponse, ToolSpec
from app.llm.fake import FakeLLM, call, say
from app.owner.notifier import RecordingNotifier
from app.scheduling.registry import default_calendars
from app.tenants.loader import load_tenant
from app.tenants.schema import StudioConfig
from tests.helpers import demo_config, kyiv

OWNER_TG = 111
ALERT_CHAT = 999


class RecordingChannel:
    """Records outgoing texts; window and limits like a real channel."""

    def __init__(self, kind: str, window: timedelta | None = None, max_bytes: int | None = None):
        self.kind = kind
        self.window = window
        self.max_bytes = max_bytes
        self.max_chars = None
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, account, recipient_id: str, text: str) -> list[str]:
        self.sent.append((recipient_id, text))
        return [f"{self.kind}-{len(self.sent)}"]

    def texts(self) -> list[str]:
        return [text for _, text in self.sent]


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(kyiv("2026-10-20 23:47"))  # Tuesday night, like the landing demo


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def app(session_factory: SessionFactory, clock: FrozenClock, llm: FakeLLM) -> AppContext:
    notifier = RecordingNotifier()
    settings = Settings(
        _env_file=None, database_url="postgresql+asyncpg://unused", alert_chat_id=ALERT_CHAT
    )
    return AppContext(
        settings=settings,
        session_factory=session_factory,
        llm=llm,
        clock=clock,
        channels={
            "cli": CLIChannel(),
            "sandbox": CLIChannel(),
            "telegram_business": RecordingChannel("telegram_business", window=timedelta(hours=24)),
            "telegram_demo": RecordingChannel("telegram_demo"),
        },
        calendars=default_calendars(),
        notifier=notifier,
        alerts=Alerts(notifier, ALERT_CHAT, clock),
    )


@dataclass
class Studio:
    tenant_id: int
    account_id: int


async def make_studio(
    app: AppContext,
    *,
    mode: str = "autopilot",
    cfg: StudioConfig | None = None,
    kind: str = "cli",
    members: tuple[int, ...] = (OWNER_TG,),
) -> Studio:
    async with app.session_factory() as session, session.begin():
        tenant = await load_tenant(session, cfg or demo_config())
        tenant.mode = mode
        for telegram_id in members:
            session.add(OwnerAccount(telegram_user_id=telegram_id, state={}))
            await session.flush()
            session.add(
                TenantMember(tenant_id=tenant.id, telegram_user_id=telegram_id, role="owner")
            )
        account = await ensure_account(session, tenant.id, kind, f"{kind}-{tenant.id}")
        return Studio(tenant.id, account.id)


async def set_tenant(app: AppContext, studio: Studio, **values: object) -> None:
    async with app.session_factory() as session, session.begin():
        tenant = await session.get(Tenant, studio.tenant_id)
        for key, value in values.items():
            setattr(tenant, key, value)


async def client_says(
    app: AppContext,
    studio: Studio,
    text: str | None,
    *,
    user: str = "client-1",
    msg_id: str | None = None,
    media_type: str | None = None,
    media: dict | None = None,
) -> int | None:
    async with app.session_factory() as session, session.begin():
        account = await session.get(ChannelAccount, studio.account_id)
        message = await record_client_message(
            session,
            account,
            Inbound(user, msg_id, text, media_type=media_type, media=media),
            app.clock.now(),
            debounce=timedelta(0),
        )
        return message.conversation_id if message else None


def reply_with_slots(template: str):
    """Scripted answer that uses the times returned by the last get_free_slots call."""

    def step(messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        result = json.loads(messages[-1].content)
        return say(template.format(*[slot["time"] for slot in result["slots"]]))

    return step


def book_slot_at(time_text: str, name: str, phone: str | None = None):
    """Scripted create_booking for the offered slot at a given time (from the system prompt)."""

    def step(messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        match = re.search(rf"(s_[\w-]+): [^;]*? о {time_text}", messages[0].content or "")
        assert match, "no offered slot at that time in the prompt"
        arguments = {"slot_id": match.group(1), "client_name": name}
        if phone:
            arguments["client_phone"] = phone
        return call("create_booking", **arguments)

    return step
