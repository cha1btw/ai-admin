from datetime import timedelta

from app.brain.guardrails import (
    build_facts,
    disclosure_needed,
    find_violations,
    strip_intro,
)
from app.brain.handoff import eta_text, in_owner_hours
from app.db.models import Conversation
from tests.helpers import demo_config, kyiv


def _facts(**kwargs):
    cfg = demo_config()
    return build_facts(
        cfg, "", offered_times=kwargs.get("times", []), extra_texts=kwargs.get("extra", [])
    )


def test_known_prices_pass_and_invented_ones_fail() -> None:
    facts = _facts()
    assert find_violations("Манікюр з гель-лаком коштує 900 грн.", facts) == []
    assert find_violations("Нарощування від 1300 грн.", facts) == []
    assert find_violations("Разом з зняттям 1100 грн.", facts) == []  # 900 + 200
    assert find_violations("Зі знижкою 450 грн.", facts) == []  # real price of another service
    assert find_violations("Для вас 750 грн.", facts) == ["сума 750"]


def test_times_must_come_from_slots_hours_or_client() -> None:
    facts = _facts(times=["15:30"], extra=["Можна після 16:15?"])
    assert find_violations("Вільно о 15:30.", facts) == []
    assert find_violations("Працюємо з 10:00 до 20:00.", facts) == []
    assert find_violations("Після 16:15 є вікно о 15:30.", facts) == []
    assert find_violations("Є вікно о 17:45.", facts) == ["час 17:45"]
    # Dates like 22.10 are not times.
    assert find_violations("У четвер, 22.10, о 15:30.", facts) == []


def test_unknown_phone_and_link_fail() -> None:
    facts = _facts()
    assert find_violations("Телефон студії +380000000000.", facts) == []
    assert find_violations("Подзвоніть на 067 123 45 67.", facts) == ["телефон …4567"]
    assert find_violations("Деталі: https://evil.example/pay", facts) == [
        "посилання https://evil.example/pay"
    ]


def test_strip_intro_removes_greeting_and_self_introduction() -> None:
    text = "Добрий вечір! Я AI-асистент студії. Манікюр коштує 900 грн."
    assert strip_intro(text) == "Манікюр коштує 900 грн."
    assert strip_intro("Вітаю, так, можна.") == "Так, можна."
    assert strip_intro("Так, на завтра є вікна.") == "Так, на завтра є вікна."


def test_disclosure_rules() -> None:
    now = kyiv("2026-10-21 12:00")
    conversation = Conversation()
    assert disclosure_needed(conversation, now, 30) == "disclosure_first"
    conversation.disclosed_at = now - timedelta(hours=1)
    conversation.last_bot_message_at = now - timedelta(minutes=5)
    assert disclosure_needed(conversation, now, 30) is None
    conversation.last_human_message_at = now - timedelta(minutes=2)  # master wrote after disclosure
    assert disclosure_needed(conversation, now, 30) == "disclosure_return"
    conversation.last_human_message_at = None
    conversation.last_bot_message_at = now - timedelta(days=31)
    assert disclosure_needed(conversation, now, 30) == "disclosure_first"


def test_eta_follows_owner_hours() -> None:
    cfg = demo_config()  # owner hours: Mon-Fri 09-21, Sat 09-19, Sunday off
    assert eta_text(cfg, kyiv("2026-10-21 10:00"), "uk") == "найближчим часом"
    assert eta_text(cfg, kyiv("2026-10-21 07:30"), "uk") == "сьогодні після 09:00"
    assert eta_text(cfg, kyiv("2026-10-20 23:47"), "uk") == "завтра після 09:00"
    # Saturday evening: Sunday is off, so Monday.
    assert eta_text(cfg, kyiv("2026-10-24 20:00"), "uk") == "у понеділок після 09:00"
    assert eta_text(cfg, kyiv("2026-10-24 20:00"), "ru") == "в понедельник после 09:00"
    assert eta_text(cfg, kyiv("2026-10-24 20:00"), "pl") == "w poniedziałek po 09:00"
    assert in_owner_hours(cfg, kyiv("2026-10-21 10:00"))
    assert not in_owner_hours(cfg, kyiv("2026-10-25 10:00"))


def test_handoff_promises_are_recognised() -> None:
    from app.brain.guardrails import mentions_health, promises_handoff

    assert promises_handoff("З питаннями про алергію ми переводимо вас на фахівця.")
    assert promises_handoff("Для знижки зверніться, будь ласка, до адміністратора.")
    assert promises_handoff("Ваше повідомлення передано, майстер відповість завтра.")
    assert promises_handoff("The studio will get back to you tomorrow.")
    assert not promises_handoff("Передоплата 300 грн, реквізити надішлемо після запису.")
    assert not promises_handoff("Майстер підбере форму під вашу довжину.")
    assert mentions_health(["У мене алергія на акрил"])
    assert not mentions_health(["Скільки коштує манікюр?"])
