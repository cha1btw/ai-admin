"""Owner Bot actions (phase 2): drafts, takeover, replies, bookings, modes, sandbox."""

from datetime import timedelta

from sqlalchemy import func, select

from app.brain.orchestrator import process_conversation
from app.db.models import (
    AuditLog,
    Block,
    Booking,
    Conversation,
    Draft,
    Feedback,
    Message,
    OwnerAccount,
    TenantMember,
)
from app.llm.fake import call, say
from app.owner import actions
from tests.helpers import demo_config_dict, kyiv
from tests.integration.conftest import (
    OWNER_TG,
    book_slot_at,
    client_says,
    make_studio,
    reply_with_slots,
)

STRANGER = 777


async def _draft_with_booking(app, llm, clock):
    studio = await make_studio(app, mode="draft", kind="telegram_business")
    clock.set(kyiv("2026-10-21 10:00"))
    llm.add(
        call(
            "get_free_slots",
            service_id="gel_manicure",
            date_from="2026-10-21",
            date_to="2026-10-21",
        ),
        reply_with_slots("Є вікно о {0}. Записати?"),
        book_slot_at("11:00", "Анна"),
    )
    cid = await client_says(app, studio, "Є вікно на гель-лак сьогодні?")
    await process_conversation(app, cid)
    cid = await client_says(app, studio, "Так, на 11:00, я Анна")
    plan = await process_conversation(app, cid)
    return studio, cid, plan.draft_id


def business_sent(app) -> list[str]:
    return app.channels["telegram_business"].texts()


async def test_approved_draft_executes_the_booking_and_sends(app, llm, clock) -> None:
    _, _, draft_id = await _draft_with_booking(app, llm, clock)
    assert business_sent(app) == []
    result = await actions.approve_draft(app, draft_id, OWNER_TG)
    assert business_sent(app)[0].startswith(
        "Вітаю! Я AI-асистент студії «Demo Nail Studio». Анна, вас записано: Манікюр з гель-лаком, "
        "середа, 21.10 о 11:00"
    )
    assert result.edits and result.edits[0][2].startswith("✅ Надіслано")
    async with app.session_factory() as session:
        booking = (await session.scalars(select(Booking))).one()
        draft = await session.get(Draft, draft_id)
    assert booking.status == "confirmed" and draft.status == "sent"
    # A second press does nothing.
    again = await actions.approve_draft(app, draft_id, OWNER_TG)
    assert "неактуальна" in again.text
    async with app.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Booking)) == 1


async def test_strangers_cannot_touch_drafts(app, llm, clock) -> None:
    _, _, draft_id = await _draft_with_booking(app, llm, clock)
    result = await actions.approve_draft(app, draft_id, STRANGER)
    assert result.text == "Чернетку не знайдено."
    assert business_sent(app) == []


async def test_draft_after_window_cannot_be_sent(app, llm, clock) -> None:
    """R11: drafts expire when the 24-hour window closes; the master gets the contact."""
    _, _, draft_id = await _draft_with_booking(app, llm, clock)
    clock.advance(hours=25)
    result = await actions.approve_draft(app, draft_id, OWNER_TG)
    assert "Вікно відповіді закрилося" in result.text
    assert business_sent(app) == []
    async with app.session_factory() as session:
        assert (await session.get(Draft, draft_id)).status == "expired"
        assert await session.scalar(select(func.count()).select_from(Booking)) == 0


async def test_slot_taken_before_approval(app, llm, clock) -> None:
    studio, _, draft_id = await _draft_with_booking(app, llm, clock)
    # Someone else books 11:00 with Olena first (another conversation, autopilot path).
    from tests.integration.test_booking import _ctx, _offer_at

    async with app.session_factory() as session, session.begin():
        ctx = await _ctx(session, studio, "olha", now=clock.now())
        from app.brain import booking as bookings

        await bookings.create_booking(
            ctx, await _offer_at(session, ctx, kyiv("2026-10-21 11:00")), "Ольга", None
        )
    result = await actions.approve_draft(app, draft_id, OWNER_TG)
    assert "зайняли" in result.text
    assert business_sent(app) == []


async def test_edit_and_reject(app, llm, clock) -> None:
    studio = await make_studio(app, mode="draft", kind="telegram_business")
    clock.set(kyiv("2026-10-21 10:00"))
    llm.add(say("Так, працюємо з 10:00."), say("Так, працюємо з 10:00."))
    cid = await client_says(app, studio, "Працюєте сьогодні?")
    first = (await process_conversation(app, cid)).draft_id
    started = await actions.start_edit_draft(app, first, OWNER_TG)
    assert "свій варіант" in started.text
    result = await actions.handle_text(app, OWNER_TG, "Так, чекаємо вас з 10:00 😊")
    assert result.text == "Надіслано."
    assert business_sent(app) == ["Так, чекаємо вас з 10:00 😊"]  # human text: no AI disclosure
    async with app.session_factory() as session:
        assert (await session.get(Conversation, cid)).state == "BOT_ACTIVE"

    cid = await client_says(app, studio, "А завтра?")
    second = (await process_conversation(app, cid)).draft_id
    rejected = await actions.reject_draft(app, second, OWNER_TG)
    assert rejected.edits == [] or "Відхилено" in rejected.edits[0][2]
    assert len(business_sent(app)) == 1


async def test_reply_from_owner_bot_takes_over(app, llm, clock) -> None:
    studio = await make_studio(app, kind="telegram_business")
    llm.add(call("handoff_to_human", reason="discount", summary="Просить знижку"))
    cid = await client_says(app, studio, "Дасте знижку?")
    await process_conversation(app, cid)
    await actions.start_reply(app, cid, OWNER_TG)
    result = await actions.handle_text(
        app, OWNER_TG, "Привіт! Це Олена. Для вас -10% на першу процедуру."
    )
    assert result.text.startswith("Надіслано")
    assert business_sent(app)[-1] == "Привіт! Це Олена. Для вас -10% на першу процедуру."
    async with app.session_factory() as session:
        conversation = await session.get(Conversation, cid)
        assert conversation.state == "HUMAN_ACTIVE"

    # Client writes: the bot stays quiet; the master returns the bot; new messages get answers.
    cid = await client_says(app, studio, "Дякую!")
    assert (await process_conversation(app, cid)).status == "skipped:human"
    await actions.return_bot(app, cid, OWNER_TG)
    llm.add(say("Будь ласка! Чекаємо вас."))
    cid = await client_says(app, studio, "А адреса яка?")
    await process_conversation(app, cid)
    # R1: after a human part, the bot discloses itself again.
    assert business_sent(app)[-1].startswith(
        "На зв'язку знову AI-асистент студії «Demo Nail Studio»."
    )


async def test_take_conversation_supersedes_drafts(app, llm, clock) -> None:
    _, cid, draft_id = await _draft_with_booking(app, llm, clock)
    await actions.take_conversation(app, cid, OWNER_TG)
    async with app.session_factory() as session:
        assert (await session.get(Draft, draft_id)).status == "superseded"
        assert (await session.get(Conversation, cid)).state == "HUMAN_ACTIVE"


async def test_pause_resume_reports_waiting_clients(app, llm) -> None:
    studio = await make_studio(app)
    await actions.pause(app, OWNER_TG)
    for user in ("a", "b"):
        cid = await client_says(app, studio, "Привіт", user=user)
        assert (await process_conversation(app, cid)).status == "skipped:paused"
    result = await actions.resume(app, OWNER_TG)
    assert "написали 2 клієнток" in result.text


async def test_mode_switch_and_audit(app) -> None:
    studio = await make_studio(app)
    result = await actions.set_mode(app, OWNER_TG, "offhours")
    assert "вночі" in result.text
    async with app.session_factory() as session:
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "tenant.mode"))
    assert audit.details == {"mode": "offhours"} and audit.tenant_id == studio.tenant_id


async def test_link_account_once(app) -> None:
    await make_studio(app, members=())
    token = await actions.create_link(app, "demo_studio")
    result = await actions.link_account(app, token, 555, "Олена")
    assert "Вітаю! Ви підключені до студії «Demo Nail Studio»" in result.text
    again = await actions.link_account(app, token, 556, "Хтось")
    assert "недійсне" in again.text
    async with app.session_factory() as session:
        members = list(await session.scalars(select(TenantMember.telegram_user_id)))
        account = await session.get(OwnerAccount, 555)
    assert members == [555] and account.active_tenant_id is not None


async def test_prepayment_flow(app, llm, clock) -> None:
    data = demo_config_dict()
    data["prepayment"] = {
        "required": True,
        "default_amount": 300,
        "deadline_hours": 12,
        "requisites_text": "Картка 0000 1111 2222 3333, Олена.",
    }
    from app.tenants.schema import StudioConfig

    studio = await make_studio(app, cfg=StudioConfig.model_validate(data), kind="telegram_business")
    llm.add(
        call(
            "get_free_slots",
            service_id="gel_manicure",
            date_from="2026-10-22",
            date_to="2026-10-22",
        ),
        reply_with_slots("У четвер є {0}."),
        book_slot_at("10:00", "Анна"),
    )
    cid = await client_says(app, studio, "Можна на четвер?")
    await process_conversation(app, cid)
    cid = await client_says(app, studio, "Так, 10:00, Анна")
    await process_conversation(app, cid)
    text = business_sent(app)[-1]
    assert text.startswith("Анна, час заброньовано")
    assert "передоплата 300 грн до" in text and "Картка 0000 1111 2222 3333, Олена." in text
    async with app.session_factory() as session:
        booking = (await session.scalars(select(Booking))).one()
    assert booking.status == "pending_prepayment"
    paid = await actions.confirm_payment(app, booking.id, OWNER_TG)
    assert "клієнтці надіслано підтвердження" in paid.text
    assert business_sent(app)[-1].startswith("Оплату отримано, запис підтверджено")


async def test_lead_capture_request_confirmed_by_master(app, llm, clock) -> None:
    data = demo_config_dict()
    data["calendar_provider"] = "lead_capture"
    from app.tenants.schema import StudioConfig

    studio = await make_studio(app, cfg=StudioConfig.model_validate(data), kind="telegram_business")
    llm.add(
        call(
            "request_booking",
            service_id="gel_manicure",
            preferred_time_text="п'ятниця, 23.10, після 15:00",
            client_name="Анна",
            client_phone="0671234567",
        )
    )
    cid = await client_says(app, studio, "Запишіть на п'ятницю після 15:00, Анна, 0671234567")
    await process_conversation(app, cid)
    assert business_sent(app)[0].endswith(
        "Анна, заявку прийнято: Манікюр з гель-лаком, бажаний час: п'ятниця, 23.10, після 15:00. "
        "Студія підтвердить точний час завтра після 09:00."
    )
    note = next(n for n in app.notifier.sent if n.text.startswith("📝 Нова заявка"))
    booking_id = int(note.buttons[0][0].data.split(":")[-1])
    await actions.start_confirm_request(app, booking_id, OWNER_TG)
    bad = await actions.handle_text(app, OWNER_TG, "коли зможу")
    assert "Не зрозумів" in bad.text
    done = await actions.handle_text(app, OWNER_TG, "23.10 15:30")
    assert done.text.startswith("Запис підтверджено")
    assert business_sent(app)[-1].startswith(
        "Студія підтвердила ваш запис: Манікюр з гель-лаком, п'ятниця, 23.10 о 15:30"
    )


async def test_feedback_goes_to_founders(app, llm) -> None:
    studio = await make_studio(app)
    llm.add(say("Так, працюємо з 10:00."))
    cid = await client_says(app, studio, "Працюєте?")
    await process_conversation(app, cid)
    async with app.session_factory() as session:
        message_id = await session.scalar(select(Message.id).where(Message.role == "bot"))
    await actions.feedback(app, message_id, OWNER_TG)
    await actions.handle_text(app, OWNER_TG, "Не привіталась як ми звикли")
    async with app.session_factory() as session:
        row = (await session.scalars(select(Feedback))).one()
    assert row.comment == "Не привіталась як ми звикли"
    assert any("👎" in text for text in app.alerts.sent)


async def test_close_days_blocks_slots(app, llm, clock) -> None:
    studio = await make_studio(app)
    clock.set(kyiv("2026-10-21 10:00"))
    result = await actions.close_days(app, OWNER_TG, "22.10 немає світла")
    assert result.text.startswith("Закрито: четвер, 22.10")
    async with app.session_factory() as session:
        block = (await session.scalars(select(Block))).one()
    assert block.reason == "немає світла"
    llm.add(
        call(
            "get_free_slots",
            service_id="gel_manicure",
            date_from="2026-10-22",
            date_to="2026-10-22",
        ),
        lambda messages, tools: say("Вільних вікон немає."),
    )
    cid = await client_says(app, studio, "Можна на четвер?")
    await process_conversation(app, cid)
    tool_result = llm.calls[1][0][-1].content
    assert '"slots": [{' in tool_result and "22.10" not in tool_result  # alternatives only
    reopened = await actions.close_days(app, OWNER_TG, "22.10", reopen=True)
    assert reopened.text.startswith("Відкрито")
    del studio


async def test_sandbox_flow(app, llm) -> None:
    await make_studio(app)
    started = await actions.start_sandbox(app, OWNER_TG)
    assert started.text.startswith("🧪 Пісочниця")
    assert await actions.handle_text(app, OWNER_TG, "Скільки коштує педикюр?") is None
    async with app.session_factory() as session:
        conversation = (await session.scalars(select(Conversation))).one()
        assert conversation.is_sandbox
    llm.add(say("Педикюр з гель-лаком коштує 1100 грн."))
    async with app.session_factory() as session, session.begin():
        row = await session.get(Conversation, conversation.id)
        row.reply_due_at = app.clock.now() - timedelta(seconds=1)
    await process_conversation(app, conversation.id)
    assert app.channels["sandbox"].texts()[0].endswith("Педикюр з гель-лаком коштує 1100 грн.")
    assert app.notifier.sent == []
    stopped = await actions.stop_sandbox(app, OWNER_TG)
    assert stopped.text == "Пісочницю закрито."


async def test_stats_text(app, llm, clock) -> None:
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
    result = await actions.show_stats(app, OWNER_TG, 7)
    assert "Записи від бота: 1 на суму 900 грн" in result.text
    assert "Діалоги: 1" in result.text
    del studio


async def test_unlinked_user_gets_instructions(app) -> None:
    await make_studio(app)
    result = await actions.show_mode(app, STRANGER)
    assert result.text == actions.NOT_LINKED
