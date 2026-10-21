"""End-to-end pipeline with a scripted model: rules R1-R13 that code must hold."""

from sqlalchemy import func, select

from app.brain.ingest import record_outgoing
from app.brain.orchestrator import process_conversation
from app.db.models import Booking, ChannelAccount, Conversation, Draft, Message
from app.llm.base import LLMTimeout
from app.llm.fake import call, say
from tests.helpers import demo_config, demo_config_dict, kyiv
from tests.integration.conftest import (
    OWNER_TG,
    book_slot_at,
    client_says,
    make_studio,
    reply_with_slots,
    set_tenant,
)

DISCLOSURE = "Вітаю! Я AI-асистент студії «Demo Nail Studio»."


def sent(app) -> list[str]:
    return app.channels["cli"].texts()


async def conversation(app, conversation_id: int) -> Conversation:
    async with app.session_factory() as session:
        return await session.get(Conversation, conversation_id)


async def test_night_booking_scene(app, llm, clock) -> None:
    """Landing scene 1: price and slots at 23:47, then a booking and a note to the master."""
    studio = await make_studio(app)
    llm.add(
        call(
            "get_free_slots",
            service_id="gel_manicure",
            date_from="2026-10-21",
            date_to="2026-10-21",
            time_from="15:00",
        ),
        reply_with_slots(
            "Добрий вечір! Манікюр з гель-лаком коштує 900 грн, триває приблизно дві години. "
            "Завтра, у середу 21.10, вільно о {0} і {1}. Який час зручніший?"
        ),
    )
    cid = await client_says(
        app,
        studio,
        "Добрий вечір! Скільки коштує манікюр з гель-лаком? Є вікно завтра після 15:00?",
    )
    plan = await process_conversation(app, cid)
    assert plan.status == "replied"
    first = sent(app)[0]
    # R1 by code: one disclosure, the model's own greeting removed.
    assert first.startswith(f"{DISCLOSURE} Манікюр з гель-лаком коштує 900 грн")
    assert first.count("Добрий вечір") == 0
    system_prompt = llm.calls[0][0][0].content
    assert "Зараз: 2026-10-20 23:47, вівторок" in system_prompt
    assert "Публічна демо-студія" not in system_prompt  # internal notes stay out (R7)

    clock.advance(minutes=1)
    llm.add(book_slot_at("15:00", "Анна"))
    cid = await client_says(app, studio, "Так, на 15:00. Я Анна")
    await process_conversation(app, cid)
    # R3: the confirmation is a template from database values, no second disclosure.
    assert sent(app)[1] == (
        "Анна, вас записано: Манікюр з гель-лаком, середа, 21.10 о 15:00. Майстер: Олена. "
        "Вартість: 900 грн. Адреса: вул. Демонстраційна, 1, Київ (вигадана адреса). "
        "Якщо плани зміняться, напишіть сюди."
    )
    async with app.session_factory() as session:
        booking = (await session.scalars(select(Booking))).one()
    assert (booking.status, booking.price_min) == ("confirmed", 900)
    note = next(n for n in app.notifier.sent if n.text.startswith("🗓 Новий запис"))
    assert note.chat_id == OWNER_TG
    assert note.silent  # 23:48 is outside the master's hours
    assert "Анна · середа, 21.10 о 15:00" in note.text


async def test_allergy_scene_hands_off_and_bot_goes_quiet(app, llm, clock) -> None:
    """Landing scene 2 (R5, R8): no medical answer, master is called, the bot stays quiet."""
    studio = await make_studio(app)
    llm.add(call("handoff_to_human", reason="medical", summary="Питає про алергію на акрил."))
    cid = await client_says(app, studio, "У мене алергія на акрил. Це небезпечно для мене?")
    await process_conversation(app, cid)
    assert sent(app) == [
        f"{DISCLOSURE} Це питання краще уточнити в студії. Ваше повідомлення вже передано, "
        "вам відповідять тут завтра після 09:00."
    ]
    assert (await conversation(app, cid)).state == "HANDOFF_PENDING"
    note = next(n for n in app.notifier.sent if n.text.startswith("🙋 Потрібна ваша відповідь"))
    assert "питання про здоров'я" in note.text
    assert [b.data for row in note.buttons for b in row] == [
        f"c:reply:{cid}",
        f"c:take:{cid}",
        f"c:bot:{cid}",
    ]

    clock.advance(minutes=5)
    cid = await client_says(app, studio, "Алло?")
    plan = await process_conversation(app, cid)
    assert plan.status == "skipped:handoff_pending"
    assert len(sent(app)) == 1 and len(llm.calls) == 1

    clock.advance(hours=13)  # one polite holding message per 12 hours
    cid = await client_says(app, studio, "Ви тут?")
    await process_conversation(app, cid)
    assert sent(app)[-1].startswith("Ваше повідомлення бачать у студії")
    assert len(llm.calls) == 1  # still no model call


async def test_master_writing_by_hand_silences_the_bot(app, llm) -> None:
    studio = await make_studio(app)
    cid = await client_says(app, studio, "Привіт, можна на завтра?")
    async with app.session_factory() as session, session.begin():
        account = await session.get(ChannelAccount, studio.account_id)
        taken = await record_outgoing(
            session,
            account,
            "client-1",
            "manual-1",
            "Привіт! Це Олена, зараз підкажу.",
            app.clock.now(),
        )
    assert taken is not None
    plan = await process_conversation(app, cid)
    assert plan.status == "skipped:human"
    assert sent(app) == [] and llm.calls == []


async def test_own_echo_does_not_count_as_human(app, llm) -> None:
    studio = await make_studio(app)
    llm.add(say("Так, працюємо з 10:00 до 20:00."))
    cid = await client_says(app, studio, "Працюєте завтра?")
    await process_conversation(app, cid)
    async with app.session_factory() as session, session.begin():
        bot_message = await session.scalar(select(Message).where(Message.role == "bot"))
        account = await session.get(ChannelAccount, studio.account_id)
        result = await record_outgoing(
            session, account, "client-1", bot_message.external_id, bot_message.text, app.clock.now()
        )
    assert result is None
    assert (await conversation(app, cid)).state == "BOT_ACTIVE"


async def test_draft_mode_has_no_side_effects(app, llm, clock) -> None:
    """R12: in draft mode nothing reaches the client and no booking exists before approval."""
    studio = await make_studio(app, mode="draft")
    clock.set(kyiv("2026-10-21 10:00"))
    llm.add(
        call(
            "get_free_slots",
            service_id="gel_manicure",
            date_from="2026-10-21",
            date_to="2026-10-21",
        ),
        reply_with_slots("Сьогодні є вікно о {0}. Записати вас?"),
    )
    cid = await client_says(app, studio, "Є вікно на гель-лак сьогодні?")
    plan = await process_conversation(app, cid)
    assert plan.status == "drafted" and sent(app) == []

    llm.add(book_slot_at("11:00", "Анна"))
    cid = await client_says(app, studio, "Так, на 11:00, я Анна")
    plan = await process_conversation(app, cid)
    assert plan.status == "drafted" and sent(app) == []
    async with app.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Booking)) == 0
        drafts = {d.id: d for d in await session.scalars(select(Draft))}
    assert [d.status for d in drafts.values()] == ["superseded", "pending"]
    pending = drafts[plan.draft_id]
    assert pending.proposed_action["tool"] == "create_booking"
    assert pending.text.startswith(
        "Анна, вас записано: Манікюр з гель-лаком, середа, 21.10 о 11:00"
    )
    note = app.notifier.sent[-1]
    assert note.text.startswith("✍️ Чернетка відповіді")
    data = [b.data for row in note.buttons for b in row]
    assert f"d:send:{pending.id}" in data and f"d:edit:{pending.id}" not in data


async def test_offhours_answers_alone_at_night_and_drafts_by_day(app, llm, clock) -> None:
    studio = await make_studio(app, mode="offhours")
    llm.add(say("Так, працюємо з 10:00."), say("Так, працюємо з 10:00."))
    cid = await client_says(app, studio, "Працюєте завтра?")  # 23:47: master is off
    assert (await process_conversation(app, cid)).status == "replied"
    clock.set(kyiv("2026-10-21 11:00"))  # master's hours and not busy
    cid = await client_says(app, studio, "А в четвер?")
    assert (await process_conversation(app, cid)).status == "drafted"


async def test_invented_price_is_regenerated_once(app, llm) -> None:
    studio = await make_studio(app)
    llm.add(
        say("Манікюр з гель-лаком коштує 700 грн."), say("Манікюр з гель-лаком коштує 900 грн.")
    )
    cid = await client_says(app, studio, "Скільки коштує гель-лак?")
    await process_conversation(app, cid)
    assert sent(app) == [f"{DISCLOSURE} Манікюр з гель-лаком коштує 900 грн."]
    correction = llm.calls[1][0][-1]
    assert correction.role == "system" and "сума 700" in correction.content


async def test_twice_invented_facts_hand_off(app, llm) -> None:
    studio = await make_studio(app)
    llm.add(say("Коштує 700 грн."), say("Коштує 750 грн."))
    cid = await client_says(app, studio, "Скільки коштує?")
    await process_conversation(app, cid)
    assert "Ваше повідомлення вже передано" in sent(app)[0]
    assert (await conversation(app, cid)).state == "HANDOFF_PENDING"


async def test_model_failure_never_leaves_client_silent(app, llm) -> None:
    studio = await make_studio(app)
    llm.add(LLMTimeout("timeout"))
    cid = await client_says(app, studio, "Скільки коштує педикюр?")
    await process_conversation(app, cid)
    assert sent(app) == [
        f"{DISCLOSURE} Дякуємо за повідомлення! Вам відповідять тут завтра після 09:00."
    ]
    assert (await conversation(app, cid)).state == "HANDOFF_PENDING"
    assert any("Модель недоступна" in text for text in app.alerts.sent)


async def test_reply_limit_stops_loops(app, llm) -> None:
    """R13: a chatty bot on the other side cannot keep ours talking forever."""
    data = demo_config_dict()
    data["limits"]["replies_per_hour"] = 2
    from app.tenants.schema import StudioConfig

    studio = await make_studio(app, cfg=StudioConfig.model_validate(data))
    llm.add(say("Так, працюємо з 10:00."), say("Так."))
    for text in ("Працюєте?", "Точно?"):
        cid = await client_says(app, studio, text)
        await process_conversation(app, cid)
    cid = await client_says(app, studio, "А ще?")
    await process_conversation(app, cid)
    assert len(llm.calls) == 2
    assert (await conversation(app, cid)).state == "HANDOFF_PENDING"
    assert any("ліміт відповідей" in text for text in app.alerts.sent)


async def test_voice_messages(app, llm) -> None:
    studio = await make_studio(app)
    cid = await client_says(app, studio, None, msg_id="v1", media_type="voice")
    await process_conversation(app, cid)
    assert sent(app)[0] == (
        f"{DISCLOSURE} Вибачте, голосові повідомлення поки не прослуховуються. "
        "Напишіть, будь ласка, текстом."
    )
    cid = await client_says(app, studio, None, msg_id="v2", media_type="voice")
    await process_conversation(app, cid)
    assert sent(app)[1].startswith("Дякуємо! Голосове вже передано в студію")
    assert (await conversation(app, cid)).state == "HANDOFF_PENDING"
    assert llm.calls == []


async def test_photo_is_forwarded_and_model_sees_a_marker(app, llm) -> None:
    studio = await make_studio(app)
    llm.add(call("handoff_to_human", reason="media", summary="Хоче такий дизайн, питає ціну."))
    cid = await client_says(
        app,
        studio,
        "Скільки буде такий дизайн?",
        msg_id="p1",
        media_type="photo",
        media={"url": "https://cdn.example/photo.jpg"},
    )
    await process_conversation(app, cid)
    user_turn = llm.calls[0][0][-1].content
    assert user_turn.startswith("[клієнтка надіслала фото] Скільки буде такий дизайн?")
    assert sent(app)[0].endswith(
        "Дякуємо за фото! Його вже передано в студію, вам відповідять тут завтра після 09:00."
    )
    assert any(n.text.startswith("[фото] 📎") for n in app.notifier.sent)


async def test_paused_studio_does_not_answer_backlog(app, llm) -> None:
    studio = await make_studio(app)
    await set_tenant(app, studio, status="paused")
    cid = await client_says(app, studio, "Привіт")
    assert (await process_conversation(app, cid)).status == "skipped:paused"
    await set_tenant(app, studio, status="active")
    assert (await process_conversation(app, cid)).status == "nothing"
    llm.add(say("Так, можна."))
    cid = await client_says(app, studio, "Можна записатися?")
    await process_conversation(app, cid)
    assert len(sent(app)) == 1
    history = [m.content for m in llm.calls[0][0][1:]]
    assert history == ["Привіт", "Можна записатися?"]  # old message kept as context only


async def test_duplicate_delivery_is_ignored(app) -> None:
    studio = await make_studio(app)
    first = await client_says(app, studio, "Привіт", msg_id="m1")
    second = await client_says(app, studio, "Привіт", msg_id="m1")
    assert first is not None and second is None


async def test_language_policy(app, llm) -> None:
    """Russian is allowed in the demo studio, so the disclosure follows the client."""
    studio = await make_studio(app)
    llm.add(say("Да, работаем с 10:00."))
    cid = await client_says(app, studio, "Здравствуйте, вы работаете завтра?")
    await process_conversation(app, cid)
    assert sent(app)[0].startswith("Здравствуйте! Я AI-ассистент студии «Demo Nail Studio».")

    data = demo_config_dict()
    data["slug"] = "ua_only"
    data["allowed_languages"] = ["uk"]
    from app.tenants.schema import StudioConfig

    other = await make_studio(app, cfg=StudioConfig.model_validate(data), members=())
    llm.add(say("Так, працюємо з 10:00."))
    cid = await client_says(app, other, "Здравствуйте, вы работаете завтра?", user="client-2")
    await process_conversation(app, cid)
    assert sent(app)[1].startswith("Вітаю! Я AI-асистент студії")
    assert "відповідай українською" in llm.calls[-1][0][0].content


async def test_privacy_notice_once_when_phone_is_asked(app, llm) -> None:
    data = demo_config_dict()
    data["profile"]["privacy_url"] = "https://example.com/privacy"
    from app.tenants.schema import StudioConfig

    studio = await make_studio(app, cfg=StudioConfig.model_validate(data))
    llm.add(
        say("Напишіть, будь ласка, ваше ім'я і номер телефону."), say("Дякую, а номер телефону?")
    )
    for text in ("Хочу записатися", "Я Анна"):
        cid = await client_says(app, studio, text)
        await process_conversation(app, cid)
    assert sent(app)[0].endswith("Як ми зберігаємо дані: https://example.com/privacy")
    assert "privacy" not in sent(app)[1]


async def test_sandbox_never_notifies(app, llm) -> None:
    studio = await make_studio(app, kind="sandbox")
    async with app.session_factory() as session, session.begin():
        account = await session.get(ChannelAccount, studio.account_id)
        from app.brain.ingest import get_or_create_conversation

        _, conv = await get_or_create_conversation(
            session, account, str(OWNER_TG), app.clock.now(), sandbox=True
        )
    llm.add(call("handoff_to_human", reason="complaint", summary="Тестова скарга"))
    from datetime import timedelta

    from app.brain.ingest import Inbound, record_client_message

    async with app.session_factory() as session, session.begin():
        account = await session.get(ChannelAccount, studio.account_id)
        await record_client_message(
            session,
            account,
            Inbound(str(OWNER_TG), None, "Хочу поскаржитися"),
            app.clock.now(),
            debounce=timedelta(0),
            sandbox=True,
        )
    await process_conversation(app, conv.id)
    assert app.channels["sandbox"].texts()
    assert app.notifier.sent == []


def test_demo_config_has_landing_faq() -> None:
    cfg = demo_config()
    assert any("короткі нігті" in f.q for f in cfg.faq)  # landing scene 1 answer exists in data


async def test_daily_token_budget_stops_model_calls(app, llm, clock) -> None:
    """R13: when the studio's daily token budget is spent, clients get the safe template."""
    studio = await make_studio(app)
    cid = await client_says(app, studio, "Привіт")
    async with app.session_factory() as session, session.begin():
        session.add(
            Message(
                tenant_id=studio.tenant_id,
                conversation_id=cid,
                role="system",
                tokens_in=400_000,
                tokens_out=1,
                created_at=clock.now(),
            )
        )
    await process_conversation(app, cid)
    assert llm.calls == []
    assert sent(app)[0].endswith("Дякуємо за повідомлення! Вам відповідять тут завтра після 09:00.")
    assert any("бюджет" in text for text in app.alerts.sent)
