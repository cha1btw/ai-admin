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
    assert result.replace and "Зараз: 🌙 Ніч і зайняті години" in result.text
    assert [b.text for row in result.buttons for b in row][:3] == [
        "✍️ Чернетка",
        "✓ 🌙 Ніч і зайняті години",
        "🚀 Автопілот",
    ]
    async with app.session_factory() as session:
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "tenant.mode"))
    assert audit.details == {"mode": "offhours"} and audit.tenant_id == studio.tenant_id


async def test_link_account_once(app) -> None:
    await make_studio(app, members=())
    token = await actions.create_link(app, "demo_studio")
    result = await actions.link_account(app, token, 555, "Олена")
    assert "Вітаю! Ви підключені до студії «Demo Nail Studio»" in result.text
    assert result.show_menu and result.buttons is None
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
    assert "клієнтці надіслано підтвердження" in paid.note
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
    assert "Не вдалося розпізнати" in bad.text
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


async def _sandbox_turn(app, text: str) -> int:
    """The master writes in the sandbox and the reply is due at once."""
    await actions.handle_text(app, OWNER_TG, text)
    async with app.session_factory() as session, session.begin():
        row = (await session.scalars(select(Conversation))).one()
        row.reply_due_at = app.clock.now()
    await process_conversation(app, row.id)
    return row.id


async def test_sandbox_explains_handoff_and_keeps_answering(app, llm) -> None:
    await make_studio(app)
    await actions.start_sandbox(app, OWNER_TG)
    llm.add(call("handoff_to_human", reason="complaint", summary="Тестова скарга"))
    cid = await _sandbox_turn(app, "Хочу поскаржитися")
    assert app.channels["sandbox"].texts()  # the client template, as in a real chat
    [note] = app.notifier.sent  # only the note in the master's own chat, no notification
    assert note.chat_id == OWNER_TG and note.text.startswith("🙋 Тут бот передав би діалог")
    assert "скарга" in note.text
    assert [b.data for b in note.buttons[0]] == ["sb:restart", "sb:stop"]
    async with app.session_factory() as session:
        assert (await session.get(Conversation, cid)).state == "BOT_ACTIVE"
    llm.add(say("Чим ще можу допомогти?"))
    await _sandbox_turn(app, "А ще питання")
    assert app.channels["sandbox"].texts()[-1].endswith("Чим ще можу допомогти?")


async def test_sandbox_example_question_buttons(app, llm) -> None:
    from app.owner import bot

    await make_studio(app)
    started = await actions.start_sandbox(app, OWNER_TG)
    data = [b.data for row in started.buttons for b in row]
    assert data == [f"sb:try:{i}" for i in range(5)] + ["sb:restart", "sb:stop"]
    assert all(len(d.encode()) <= 64 for d in data)
    llm.add(say("Манікюр з гель-лаком коштує 900 грн."))
    shown = await bot._sandbox_button(app, OWNER_TG, "try:0")
    assert shown.text == "👤 Які у вас послуги й ціни?"  # the question appears in the chat
    async with app.session_factory() as session:
        conversation = (await session.scalars(select(Conversation))).one()
        assert conversation.is_sandbox and conversation.reply_due_at is not None
    await process_conversation(app, conversation.id)
    assert app.channels["sandbox"].texts()[-1].endswith("900 грн.")
    # Outside the sandbox the button only explains what to do first.
    await actions.stop_sandbox(app, OWNER_TG)
    outside = await actions.sandbox_try(app, OWNER_TG, 0)
    assert outside.text is None and "пісочницю" in outside.toast
    assert (await actions.sandbox_try(app, OWNER_TG, 99)).text is None


async def test_sandbox_works_while_paused(app, llm) -> None:
    await make_studio(app)
    await actions.pause(app, OWNER_TG)
    await actions.start_sandbox(app, OWNER_TG)
    llm.add(say("Манікюр з гель-лаком коштує 900 грн."))
    await _sandbox_turn(app, "Скільки коштує манікюр?")
    assert app.channels["sandbox"].texts()[-1].endswith("900 грн.")


async def test_sandbox_shows_typing(app, llm) -> None:
    from app.worker.main import process_due_replies

    shown: list[str] = []

    async def typing(account, recipient_id: str) -> None:
        shown.append(recipient_id)

    app.channels["sandbox"].typing = typing
    await make_studio(app)
    await actions.start_sandbox(app, OWNER_TG)
    await actions.handle_text(app, OWNER_TG, "Привіт")
    assert shown == [str(OWNER_TG)]  # right away, before the reply is due
    llm.add(say("Вітаю! Чим допомогти?"))
    async with app.session_factory() as session, session.begin():
        row = (await session.scalars(select(Conversation))).one()
        row.reply_due_at = app.clock.now()
    assert await process_due_replies(app) == 1
    assert len(shown) >= 1 and app.channels["sandbox"].texts()


async def test_status_screen_follows_pause(app) -> None:
    await make_studio(app)
    home = await actions.home(app, OWNER_TG)
    assert home.text.startswith("🏠 Demo Nail Studio") and "🟢 Бот працює" in home.text
    assert "Записів через бота сьогодні: 0" in home.text
    assert home.buttons is None  # the actions are the keyboard under the input field
    await actions.pause(app, OWNER_TG)
    assert "⏸ Бот на паузі" in (await actions.home(app, OWNER_TG)).text
    assert (await actions.home(app, 777)).text == actions.NOT_LINKED


async def test_take_and_return_edit_the_pressed_message(app) -> None:
    studio = await make_studio(app, kind="telegram_business")
    cid = await client_says(app, studio, "Привіт")
    taken = await actions.take_conversation(app, cid, OWNER_TG)
    assert taken.text is None and "Діалог ваш" in taken.note
    assert [b.data for row in taken.buttons for b in row] == [f"c:reply:{cid}", f"c:bot:{cid}"]
    back = await actions.return_bot(app, cid, OWNER_TG)
    assert "лише на нові" in back.note and back.toast


async def test_cancel_booking_question_can_be_declined(app) -> None:
    kept = await actions.keep_booking(app, 1, OWNER_TG)
    assert kept.replace and kept.text.startswith("Добре")


async def test_buttons_edit_in_place(app) -> None:
    """The pressed message changes (no new message, no stale keyboard) and a toast shows."""
    from types import SimpleNamespace

    from app.owner.bot import answer_button

    answers: list[str | None] = []

    async def answer(text=None):
        answers.append(text)

    message = SimpleNamespace(
        text="🙋 Потрібна ваша відповідь", chat=SimpleNamespace(id=OWNER_TG), message_id=42
    )
    query = SimpleNamespace(answer=answer, message=message)
    await make_studio(app)
    result = actions.Result(
        None,
        note="✋ Діалог ваш.",
        buttons=[[actions.Button("🤖 Повернути бота", "c:bot:1")]],
        toast="Діалог ваш",
    )
    await answer_button(app, query, result)
    assert answers == ["Діалог ваш"] and app.notifier.sent == []
    assert app.notifier.edits == [(OWNER_TG, 42, "🙋 Потрібна ваша відповідь\n\n✋ Діалог ваш.")]
    # Plain results still arrive as a new message.
    await answer_button(app, query, actions.Result("Готово."))
    assert app.notifier.texts() == ["Готово."]


def _tg_message(text: str, user: int = OWNER_TG):
    from types import SimpleNamespace

    return SimpleNamespace(
        text=text, chat=SimpleNamespace(id=user), from_user=SimpleNamespace(id=user)
    )


async def test_keyboard_labels_match_handlers() -> None:
    from app.owner.bot import MENU_BUTTONS
    from app.owner.notifier import MENU_LABELS

    labels = {label for row in MENU_LABELS for label in row}
    assert labels <= set(MENU_BUTTONS)
    assert set(MENU_BUTTONS) - labels == {"🏠 Меню"}  # old keyboard version, kept working


async def test_start_pins_the_menu_keyboard_then_shows_home(app) -> None:
    from app.owner import bot

    await make_studio(app)
    await bot.start(_tg_message("/start"), app)
    assert app.notifier.menu_shown == [OWNER_TG]
    assert app.notifier.sent[0].text.startswith("👇 Кнопки меню")
    assert app.notifier.sent[1].text.startswith("🏠 Demo Nail Studio")
    # Someone who is not linked gets no keyboard, only the instructions with their id.
    await bot.start(_tg_message("/start", user=555), app)
    assert app.notifier.menu_shown == [OWNER_TG]


async def test_keyboard_button_runs_the_action_even_in_the_sandbox(app) -> None:
    from app.owner import bot

    await make_studio(app)
    await actions.start_sandbox(app, OWNER_TG)
    await bot.on_menu_button(_tg_message("📅 Сьогодні"), app)
    assert app.notifier.sent[-1].text.startswith("Записи через бота на")
    await bot.on_menu_button(_tg_message("📌 Стан"), app)
    assert app.notifier.sent[-1].text.startswith("🏠 Demo Nail Studio")
    async with app.session_factory() as session:  # nothing went to the bot as a client's text
        assert await session.scalar(select(func.count()).select_from(Message)) == 0


async def test_every_keyboard_button_answers(app) -> None:
    from app.owner import bot
    from app.owner.notifier import MENU_LABELS

    await make_studio(app)
    for label in [label for row in MENU_LABELS for label in row]:
        before = len(app.notifier.sent)
        await bot.on_menu_button(_tg_message(label), app)
        assert len(app.notifier.sent) == before + 1, label
    texts = app.notifier.texts()
    assert any(t.startswith("Що знає бот") for t in texts)
    assert any(t.startswith("⏸ Бот на паузі") for t in texts)
    assert any(t.startswith("▶️ Бот знову працює") for t in texts)
    assert any("що змінилося" in t for t in texts)  # the /change prompt
    async with app.session_factory() as session:
        assert (await session.get(OwnerAccount, OWNER_TG)).state["awaiting"]["kind"] == "change"


async def test_admin_command_gives_a_link_only_to_founders(app, monkeypatch, tmp_path) -> None:
    from pydantic import SecretStr

    from app.admin import auth
    from app.owner import bot

    file = tmp_path / ".public_url"
    monkeypatch.setattr(auth, "PUBLIC_URL_FILE", file)
    app.settings = app.settings.model_copy(
        update={"admin_telegram_ids": [OWNER_TG], "encryption_key": SecretStr("unit-test-key")}
    )
    await make_studio(app)
    await bot.admin_panel(_tg_message("/admin"), app)
    assert "недоступна ззовні" in app.notifier.sent[-1].text  # no tunnel, no address
    file.write_text("https://demo.trycloudflare.com")
    await bot.admin_panel(_tg_message("/admin"), app)
    assert (
        app.notifier.sent[-1]
        .text.splitlines()[1]
        .startswith("https://demo.trycloudflare.com/admin/login?token=")
    )
    sent_before = len(app.notifier.sent)
    await bot.admin_panel(_tg_message("/admin", user=777), app)  # a master, not a founder
    assert len(app.notifier.sent) == sent_before


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
