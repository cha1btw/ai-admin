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


async def _bot_booking_tuesday_night(app, llm):
    """The clock fixture starts on Tuesday 2026-10-20 23:47, outside the master's hours."""
    studio = await make_studio(app)
    llm.add(
        call(
            "get_free_slots",
            service_id="gel_manicure",
            date_from="2026-10-21",
            date_to="2026-10-21",
        ),
        reply_with_slots("Є {0}."),
        book_slot_at("10:00", "Анна"),
    )
    cid = await client_says(app, studio, "Можна завтра?")
    await process_conversation(app, cid)
    cid = await client_says(app, studio, "10:00, Анна")
    await process_conversation(app, cid)
    return studio


async def test_weekly_report_on_monday_once(app, llm, clock) -> None:
    await _bot_booking_tuesday_night(app, llm)
    clock.set(kyiv("2026-10-26 08:30"))  # Monday before the master's hours
    await jobs.weekly_reports(app)
    assert not any(n.text.startswith("📊") for n in app.notifier.sent)
    clock.set(kyiv("2026-10-26 09:05"))
    await jobs.weekly_reports(app)
    await jobs.weekly_reports(app)
    reports = [n for n in app.notifier.sent if n.text.startswith("📊")]
    assert len(reports) == 1
    assert "Записів від бота: 1 на суму 900 грн" in reports[0].text
    assert "поки ви відпочивали: 1" in reports[0].text


async def test_founders_digest_with_cost(app, llm, clock) -> None:
    app.settings = app.settings.model_copy(
        update={
            "admin_telegram_ids": [777],
            "llm_price_input_per_m": 1.0,
            "llm_price_output_per_m": 4.0,
        }
    )
    await _bot_booking_tuesday_night(app, llm)
    clock.set(kyiv("2026-10-21 08:00"))
    await jobs.founders_digest(app)
    assert not [n for n in app.notifier.sent if n.chat_id == 777]
    clock.set(kyiv("2026-10-21 09:00"))
    await jobs.founders_digest(app)
    await jobs.founders_digest(app)
    digests = [n for n in app.notifier.sent if n.chat_id == 777]
    assert len(digests) == 1 and digests[0].silent
    assert "Demo Nail Studio (autopilot): діалогів 1, записів 1" in digests[0].text
    assert "≈ $" in digests[0].text


async def test_data_request_reaches_founders_with_forget_hint(app, llm, clock) -> None:
    app.settings = app.settings.model_copy(update={"admin_telegram_ids": [777]})
    studio = await make_studio(app)
    llm.add(call("handoff_to_human", reason="data_request", summary="Просить видалити дані"))
    cid = await client_says(app, studio, "Видаліть мої дані")
    await process_conversation(app, cid)
    async with app.session_factory() as session:
        client_id = (await session.get(Conversation, cid)).client_id
    founders = [n.text for n in app.notifier.sent if n.chat_id == 777]
    assert founders and f"/forget {client_id}" in founders[0]
    master = [n.text for n in app.notifier.sent if n.chat_id == OWNER_TG]
    assert "видалимо дані" in master[0]


async def test_change_request_goes_to_founders(app) -> None:
    app.settings = app.settings.model_copy(update={"admin_telegram_ids": [777]})
    await make_studio(app)
    prompt = await actions.start_change(app, OWNER_TG, None)
    assert "що змінилося" in prompt.text
    done = await actions.handle_text(app, OWNER_TG, "Манікюр тепер 950 грн")
    assert "передали команді" in done.text
    sent = [n.text for n in app.notifier.sent if n.chat_id == 777]
    assert "Манікюр тепер 950 грн" in sent[0] and "Demo Nail Studio" in sent[0]
    # The next text is not swallowed by the finished /change.
    again = await actions.handle_text(app, OWNER_TG, "привіт")
    assert again.text.startswith("Не зрозумів")
