"""Checks on the model's reply text.

R2: every amount, time, phone number and link in a reply must come from the studio data, the
offered slots or the client's own words; otherwise the reply is regenerated once and then
replaced by a safe template. R1: the disclosure is added by code, so the model's own greeting
and self-introduction are removed to avoid saying hello twice.
"""

import itertools
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.brain.phones import phone_digits
from app.db.models import Conversation
from app.tenants.schema import StudioConfig

_MONEY = re.compile(
    r"(\d[\d ]{0,7}\d|\d)\s*(?:грн|гривень|гривні|гривня|гривен|₴|uah|zł|zl|pln|eur|€|євро|евро|"
    r"czk|kč|крон)",
    re.IGNORECASE,
)
_TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_PHONE = re.compile(r"\+?\d[\d \-()]{7,}\d")
_URL = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)

_GREETING = re.compile(
    r"^\s*(добр(?:ий|ого|ої)\s+(?:день|вечір|вечора|ранок|ранку)|вітаю|привіт(?:ик)?|"
    r"здравствуйте|добр(?:ый|ое|ого)\s+(?:день|вечер|утро|утра)|привет|hello|hi|hey|"
    r"good\s+(?:morning|afternoon|evening)|dzień\s+dobry|cześć|witam)\s*[!,.]*\s*",
    re.IGNORECASE,
)
_SELF_INTRO = re.compile(
    r"^\s*(?:я|i'?m|i am|jestem)\b[^.!?]{0,60}?(?:\bai\b|\bаі\b|\bші\b|асистент|ассистент|"
    r"assistant|asystent)[^.!?]*[.!?]\s*",
    re.IGNORECASE,
)


@dataclass
class GuardFacts:
    amounts: set[int] = field(default_factory=set)
    times: set[str] = field(default_factory=set)  # "HH:MM"
    phones: set[str] = field(default_factory=set)  # last 9 digits
    urls: set[str] = field(default_factory=set)


def amounts_in(text: str) -> list[int]:
    return [int(m.group(1).replace(" ", "")) for m in _MONEY.finditer(text)]


def times_in(text: str) -> list[str]:
    return [f"{int(m.group(1)):02d}:{m.group(2)}" for m in _TIME.finditer(text)]


def phones_in(text: str) -> list[str]:
    found = []
    for match in _PHONE.finditer(text):
        digits = phone_digits(match.group(0))
        if len(digits) >= 9:
            found.append(digits[-9:])
    return found


def urls_in(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;:!?").rstrip("/") for m in _URL.finditer(text)]


def build_facts(
    cfg: StudioConfig,
    public_text: str,
    *,
    offered_times: list[str],
    extra_texts: list[str],
) -> GuardFacts:
    """Collect what the reply may mention. extra_texts: recent client messages and tool results."""
    facts = GuardFacts()
    fixed_like: list[int] = []
    for service in cfg.services:
        for amount in (service.price_min, service.price_max, cfg.prepayment_for(service)):
            if amount is not None:
                facts.amounts.add(amount)
        if service.price_min is not None:
            fixed_like.append(service.price_min)
    # A reply may add up two services ("манікюр і зняття разом 1100 грн").
    for a, b in itertools.combinations(fixed_like, 2):
        facts.amounts.add(a + b)
    if cfg.prepayment.default_amount:
        facts.amounts.add(cfg.prepayment.default_amount)

    hour_sets = [cfg.hours, cfg.effective_owner_hours]
    for weekly in hour_sets:
        for day in range(7):
            for rng in weekly.for_weekday(day):
                facts.times.update({f"{rng.start:%H:%M}", f"{rng.end:%H:%M}"})
    for rng in cfg.breaks:
        facts.times.update({f"{rng.start:%H:%M}", f"{rng.end:%H:%M}"})
    for exception in cfg.exceptions:
        for rng in exception.hours or []:
            facts.times.update({f"{rng.start:%H:%M}", f"{rng.end:%H:%M}"})
    facts.times.update(offered_times)

    if cfg.profile.phone:
        facts.phones.add(phone_digits(cfg.profile.phone)[-9:])
    for url in (
        cfg.profile.map_url,
        cfg.profile.instagram_url,
        cfg.profile.portfolio_url,
        cfg.profile.privacy_url,
    ):
        if url:
            facts.urls.add(str(url).rstrip("/"))

    for text in [public_text, *extra_texts]:
        facts.amounts.update(amounts_in(text))
        facts.times.update(times_in(text))
        facts.phones.update(phones_in(text))
        facts.urls.update(urls_in(text))
    return facts


def find_violations(text: str, facts: GuardFacts) -> list[str]:
    problems = [f"сума {a}" for a in amounts_in(text) if a not in facts.amounts]
    problems += [f"час {t}" for t in times_in(text) if t not in facts.times]
    problems += [f"телефон …{p[-4:]}" for p in phones_in(text) if p not in facts.phones]
    problems += [f"посилання {u}" for u in urls_in(text) if u not in facts.urls]
    return problems


def strip_intro(text: str) -> str:
    """Drop a leading greeting and 'I'm the AI assistant…' sentence; code adds the disclosure."""
    previous = None
    while previous != text:
        previous = text
        text = _GREETING.sub("", text, count=1)
        text = _SELF_INTRO.sub("", text, count=1)
    text = text.strip()
    return text[:1].upper() + text[1:] if text else text


def disclosure_needed(conversation: Conversation, now: datetime, silence_days: int) -> str | None:
    """Which disclosure template the next AI message needs (R1), or None."""
    if conversation.disclosed_at is None:
        return "disclosure_first"
    if (
        conversation.last_human_message_at
        # >=: if a human wrote at the same moment, disclosing again is the honest choice.
        and conversation.last_human_message_at >= conversation.disclosed_at
    ):
        return "disclosure_return"
    last = conversation.last_bot_message_at or conversation.disclosed_at
    if now - last > timedelta(days=silence_days):
        return "disclosure_first"
    return None
