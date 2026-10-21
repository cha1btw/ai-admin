"""Telegram updates through the real aiogram dispatchers (no network: handlers here do not call
the Bot API) and the webhook endpoint."""

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.main import create_app
from app.brain.orchestrator import process_conversation
from app.channels.telegram_demo import build_demo_dispatcher
from app.config import Settings
from app.core.inbox import store_event
from app.db.models import ChannelAccount, Conversation, InboundEvent, Message, OwnerAccount
from app.llm.fake import say
from app.owner.bot import build_owner_dispatcher
from app.runtime import Bots
from app.worker.main import process_inbound
from tests.integration.conftest import OWNER_TG, make_studio

CLIENT_TG = 222
DATE = 1761000000


def bots() -> Bots:
    from aiogram import Bot

    return Bots(
        owner=Bot(token="123456:TEST-OWNER"),
        demo=Bot(token="654321:TEST-DEMO"),
        owner_dispatcher=build_owner_dispatcher(),
        demo_dispatcher=build_demo_dispatcher(),
    )


def connection_update(update_id: int, *, owner: int = OWNER_TG, enabled: bool = True) -> dict:
    return {
        "update_id": update_id,
        "business_connection": {
            "id": "bc-1",
            "user": {"id": owner, "is_bot": False, "first_name": "Олена"},
            "user_chat_id": owner,
            "date": DATE,
            "is_enabled": enabled,
            "rights": {"can_reply": True},
        },
    }


def business_message(
    update_id: int, text: str, *, sender: int = CLIENT_TG, bot_sent: bool = False
) -> dict:
    message = {
        "message_id": 100 + update_id,
        "business_connection_id": "bc-1",
        "date": DATE,
        "chat": {"id": CLIENT_TG, "type": "private", "first_name": "Анна"},
        "from": {"id": sender, "is_bot": False, "first_name": "X", "language_code": "uk"},
        "text": text,
    }
    if bot_sent:
        message["sender_business_bot"] = {"id": 123456, "is_bot": True, "first_name": "Owner Bot"}
    return {"update_id": update_id, "business_message": message}


async def feed(app, payloads: list[tuple[str, dict]]) -> None:
    async with app.session_factory() as session, session.begin():
        for source, payload in payloads:
            await store_event(session, source, str(payload["update_id"]), payload)
    processed = await process_inbound(app, bots())
    assert processed == len(payloads)
    async with app.session_factory() as session:
        statuses = list(await session.scalars(select(InboundEvent.status)))
    assert set(statuses) == {"done"}, statuses


async def test_business_connection_binds_to_linked_master(app) -> None:
    studio = await make_studio(app)
    await feed(app, [("telegram_owner", connection_update(1))])
    async with app.session_factory() as session:
        account = await session.scalar(
            select(ChannelAccount).where(ChannelAccount.kind == "telegram_business")
        )
    assert account.tenant_id == studio.tenant_id and account.status == "active"
    assert account.meta["user_id"] == OWNER_TG
    assert app.notifier.sent[-1].text.startswith("✅ Бот підключено до вашого Telegram")


async def test_unknown_master_gets_instructions_and_pending_state(app) -> None:
    await make_studio(app, members=())
    await feed(app, [("telegram_owner", connection_update(1, owner=333))])
    async with app.session_factory() as session:
        owner = await session.get(OwnerAccount, 333)
        accounts = await session.scalar(select(func.count()).select_from(ChannelAccount))
    assert owner.state["pending_business_connection"]["id"] == "bc-1"
    assert accounts == 1  # only the CLI account of the studio
    assert "ще не прив'язаний" in app.notifier.sent[-1].text


async def test_client_message_then_owner_writes_by_hand(app, llm) -> None:
    await make_studio(app)
    await feed(app, [("telegram_owner", connection_update(1))])
    await feed(app, [("telegram_owner", business_message(2, "Скільки коштує манікюр?"))])
    async with app.session_factory() as session:
        conversation = (
            await session.scalars(
                select(Conversation)
                .join(ChannelAccount)
                .where(ChannelAccount.kind == "telegram_business")
            )
        ).one()
    llm.add(say("Манікюр з гель-лаком коштує 900 грн."))
    await process_conversation(app, conversation.id)
    assert app.channels["telegram_business"].texts()[0].endswith("коштує 900 грн.")

    # Our own reply comes back with sender_business_bot: not a human.
    await feed(app, [("telegram_owner", business_message(3, "x", sender=OWNER_TG, bot_sent=True))])
    async with app.session_factory() as session:
        assert (await session.get(Conversation, conversation.id)).state == "BOT_ACTIVE"
    # The master writes by hand: R8.
    await feed(app, [("telegram_owner", business_message(4, "Це Олена, підкажу", sender=OWNER_TG))])
    async with app.session_factory() as session:
        assert (await session.get(Conversation, conversation.id)).state == "HUMAN_ACTIVE"
        human = await session.scalar(select(Message).where(Message.role == "human"))
    assert human.text == "Це Олена, підкажу"


async def test_disabled_connection_ignores_messages(app) -> None:
    await make_studio(app)
    await feed(app, [("telegram_owner", connection_update(1, enabled=False))])
    await feed(app, [("telegram_owner", business_message(2, "Привіт"))])
    async with app.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0


async def test_demo_start_greets_with_disclosure(app, llm) -> None:
    studio = await make_studio(app, members=())
    del studio
    start = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": DATE,
            "chat": {"id": CLIENT_TG, "type": "private", "first_name": "Анна"},
            "from": {"id": CLIENT_TG, "is_bot": False, "first_name": "Анна", "language_code": "en"},
            "text": "/start",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        },
    }
    await feed(app, [("telegram_demo", start)])
    assert app.channels["telegram_demo"].texts() == [
        "Hello! I'm the AI assistant of the demo studio Demo Nail Studio. Ask about prices, free "
        "time or book a visit. This is a demo: the data is made up and bookings are for testing."
    ]
    hello = {**start, "update_id": 2}
    hello["message"] = {**start["message"], "message_id": 2, "text": "How much is a gel manicure?"}
    hello["message"].pop("entities")
    await feed(app, [("telegram_demo", hello)])
    async with app.session_factory() as session:
        conversation = (await session.scalars(select(Conversation))).one()
    llm.add(say("A gel manicure costs 900 UAH and takes about two hours."))
    await process_conversation(app, conversation.id)
    # Already disclosed by the greeting: no second disclosure.
    assert (
        app.channels["telegram_demo"].texts()[1]
        == "A gel manicure costs 900 UAH and takes about two hours."
    )


def test_webhook_checks_secret_and_dedupes(database_url: str) -> None:
    settings = Settings(_env_file=None, database_url=database_url, telegram_webhook_secret="s3cret")
    with TestClient(create_app(settings)) as client:
        payload = {"update_id": 42, "message": {"text": "hi"}}
        assert client.post("/webhooks/telegram/owner", json=payload).status_code == 403
        headers = {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
        for _ in range(2):
            assert (
                client.post("/webhooks/telegram/owner", json=payload, headers=headers).status_code
                == 200
            )
        assert (
            client.post("/webhooks/telegram/other", json=payload, headers=headers).status_code
            == 404
        )


async def test_webhook_rows_are_unique(app) -> None:
    async with app.session_factory() as session, session.begin():
        assert await store_event(session, "telegram_owner", "1", {"update_id": 1})
        assert not await store_event(session, "telegram_owner", "1", {"update_id": 1})


async def test_failing_event_is_retried_with_backoff_then_alerted(app, clock) -> None:
    async with app.session_factory() as session, session.begin():
        await store_event(session, "unknown_source", "1", {"x": 1})
    assert await process_inbound(app, bots()) == 1
    assert await process_inbound(app, bots()) == 0  # waits 30 s before the next attempt
    for _ in range(4):
        clock.advance(minutes=3)
        assert await process_inbound(app, bots()) == 1
    async with app.session_factory() as session:
        event = (await session.scalars(select(InboundEvent))).one()
    assert (event.status, event.attempts) == ("failed", 5)
    assert any("unknown_source" in text for text in app.alerts.sent)
