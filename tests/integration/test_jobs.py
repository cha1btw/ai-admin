"""Periodic jobs and privacy (R8, R9, R11)."""

from datetime import timedelta

from sqlalchemy import func, select

from app.brain.orchestrator import process_conversation
from app.db.models import Booking, Client, Conversation, Draft, Message, Tenant
from app.llm.fake import call, say
from app.owner import actions
from app.privacy.deletion import forget_client
from app.privacy.retention import apply_retention
from app.worker import jobs
from tests.helpers import demo_config_dict, kyiv
from tests.integration.conftest import (
    OWNER_TG,
    book_slot_at,
    client_says,
    make_studio,
    reply_with_slots,
)


async def _handoff_at_ten(app, llm, clock):
    studio = await make_studio(app, kind="telegram_business")
    clock.set(kyiv("2026-10-21 10:00"))
    llm.add(call("handoff_to_human", reason="complaint", summary="Скарга на дизайн"))
    cid = await client_says(app, studio, "Мені не сподобався дизайн")
    await process_conversation(app, cid)
    return cid


async def test_handoff_reminder_once_in_owner_hours(app, llm, clock) -> None:
    await _handoff_at_ten(app, llm, clock)
    clock.advance(minutes=10)
    await jobs.handoff_reminders(app)
    assert not any(n.text.startswith("⏰") for n in app.notifier.sent)
    clock.advance(minutes=21)
    await jobs.handoff_reminders(app)
    await jobs.handoff_reminders(app)
    reminders = [n for n in app.notifier.sent if n.text.startswith("⏰")]
    assert len(reminders) == 1 and "чекає на вашу відповідь уже 31 хв" in reminders[0].text


async def test_bot_returns_after_resume_hours(app, llm, clock) -> None:
    cid = await _handoff_at_ten(app, llm, clock)
    await actions.take_conversation(app, cid, OWNER_TG)
    clock.advance(hours=11)
    await jobs.resume_human_conversations(app)
    async with app.session_factory() as session:
        assert (await session.get(Conversation, cid)).state == "HUMAN_ACTIVE"
    clock.advance(hours=2)  # 13 h > resume_hours (12)
    await jobs.resume_human_conversations(app)
    async with app.session_factory() as session:
        assert (await session.get(Conversation, cid)).state == "BOT_ACTIVE"


async def test_drafts_expire_with_the_window(app, llm, clock) -> None:
    studio = await make_studio(app, mode="draft", kind="telegram_business")
    clock.set(kyiv("2026-10-21 10:00"))
    llm.add(say("Так, працюємо з 10:00."))
    cid = await client_says(app, studio, "Працюєте?")
    draft_id = (await process_conversation(app, cid)).draft_id
    clock.advance(hours=23)
    await jobs.expire_drafts(app)
    async with app.session_factory() as session:
        assert (await session.get(Draft, draft_id)).status == "pending"
    clock.advance(hours=2)
    await jobs.expire_drafts(app)
    async with app.session_factory() as session:
        assert (await session.get(Draft, draft_id)).status == "expired"
    assert app.notifier.edits and "Чернетка застаріла" in app.notifier.edits[-1][2]


async def test_prepayment_reminder(app, llm, clock) -> None:
    data = demo_config_dict()
    data["prepayment"] = {
        "required": True,
        "default_amount": 300,
        "deadline_hours": 2,
        "requisites_text": "Картка 0000.",
    }
    from app.tenants.schema import StudioConfig

    studio = await make_studio(app, cfg=StudioConfig.model_validate(data))
    clock.set(kyiv("2026-10-21 10:00"))
    llm.add(
        call(
            "get_free_slots",
            service_id="gel_manicure",
            date_from="2026-10-23",
            date_to="2026-10-23",
        ),
        reply_with_slots("Є {0}."),
        book_slot_at("10:00", "Анна"),
    )
    cid = await client_says(app, studio, "Можна в п'ятницю?")
    await process_conversation(app, cid)
    cid = await client_says(app, studio, "10:00, Анна")
    await process_conversation(app, cid)
    clock.advance(hours=3)
    await jobs.prepayment_reminders(app)
    await jobs.prepayment_reminders(app)
    reminders = [n for n in app.notifier.sent if n.text.startswith("⏳ Минув термін передоплати")]
    assert len(reminders) == 1
    async with app.session_factory() as session:
        assert (await session.scalars(select(Booking))).one().status == "pending_prepayment"


async def test_morning_digest_once_a_day(app, llm, clock) -> None:
    await _handoff_at_ten(app, llm, clock)
    clock.set(kyiv("2026-10-22 08:30"))
    await jobs.morning_digests(app)
    assert not any(n.text.startswith("☀️") for n in app.notifier.sent)
    clock.set(kyiv("2026-10-22 09:05"))
    await jobs.morning_digests(app)
    await jobs.morning_digests(app)
    digests = [n for n in app.notifier.sent if n.text.startswith("☀️")]
    assert len(digests) == 1 and "Чекають на вашу відповідь: 1" in digests[0].text


async def test_demo_cleanup(app, llm, clock) -> None:
    studio = await make_studio(app, members=())
    async with app.session_factory() as session, session.begin():
        (await session.get(Tenant, studio.tenant_id)).is_demo = True
    llm.add(
        call(
            "get_free_slots", service_id="gel_removal", date_from="2026-10-21", date_to="2026-10-21"
        ),
        reply_with_slots("Є {0}."),
        book_slot_at("10:00", "Тест"),
    )
    cid = await client_says(app, studio, "Зняття завтра?")
    await process_conversation(app, cid)
    cid = await client_says(app, studio, "10:00, Тест")
    await process_conversation(app, cid)
    async with app.session_factory() as session, session.begin():
        booking = (await session.scalars(select(Booking))).one()
        booking.created_at = clock.now() - timedelta(days=2)
    await jobs.demo_cleanup(app)
    async with app.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Booking)) == 0


async def test_retention_deletes_old_conversations_and_anonymizes_bookings(app, llm, clock) -> None:
    studio = await make_studio(app)
    llm.add(
        call(
            "get_free_slots", service_id="gel_removal", date_from="2026-10-21", date_to="2026-10-21"
        ),
        reply_with_slots("Є {0}."),
        book_slot_at("10:00", "Анна"),
    )
    cid = await client_says(app, studio, "Зняття завтра?")
    await process_conversation(app, cid)
    cid = await client_says(app, studio, "10:00, Анна")
    await process_conversation(app, cid)
    async with app.session_factory() as session, session.begin():
        await session.execute(
            Message.__table__.update().values(created_at=clock.now() - timedelta(days=91))
        )
        client = (await session.scalars(select(Client))).one()
        client.created_at = client.last_seen_at = clock.now() - timedelta(days=91)
    clock.advance(days=95)  # the visit on 21.10 is long past
    counts = await apply_retention(app)
    assert counts["messages"] >= 4 and counts["bookings_anonymized"] == 1 and counts["clients"] == 1
    async with app.session_factory() as session:
        booking = (await session.scalars(select(Booking))).one()
    assert booking.client_name is None and booking.price_min == 200  # statistics survive


async def test_forget_client(app, llm, clock) -> None:
    studio = await make_studio(app)
    llm.add(say("Так, працюємо з 10:00."))
    cid = await client_says(app, studio, "Працюєте? Мене звати Анна, 0671234567")
    await process_conversation(app, cid)
    async with app.session_factory() as session:
        client_id = (await session.scalars(select(Client.id))).one()
    assert await forget_client(app, client_id, actor="admin:1")
    async with app.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 0
    assert not await forget_client(app, client_id, actor="admin:1")
