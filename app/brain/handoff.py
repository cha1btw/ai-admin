"""Handing a conversation to a human (R8) and the honest answer to "when will they reply?"."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.brain.templates import eta_phrase
from app.db.models import Conversation
from app.tenants.schema import StudioConfig, TimeRange

HANDOFF_REASONS = (
    "medical",
    "complaint",
    "discount",
    "refund",
    "unknown_fact",
    "special_request",
    "media",
    "price_dispute",
    "data_request",
    "minor",
    "abuse",
    "other",
)

REASON_LABELS = {
    "medical": "питання про здоров'я",
    "complaint": "скарга",
    "discount": "знижка",
    "refund": "повернення коштів",
    "unknown_fact": "немає відповіді в даних",
    "special_request": "особливе прохання",
    "media": "фото або голосове",
    "price_dispute": "суперечка щодо ціни",
    "data_request": "запит на видалення даних",
    "minor": "неповнолітня клієнтка",
    "abuse": "ліміти або дивна поведінка",
    "other": "інше",
}


def owner_ranges(cfg: StudioConfig, day: date) -> list[TimeRange]:
    exception = cfg.exception_for(day)
    if exception is not None and exception.closed:
        return []
    return cfg.effective_owner_hours.for_weekday(day.weekday())


def in_owner_hours(cfg: StudioConfig, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(cfg.timezone))
    return any(r.start <= local.time() < r.end for r in owner_ranges(cfg, local.date()))


def eta_text(cfg: StudioConfig, now: datetime, lang: str) -> str:
    """Honest reply time: 'найближчим часом', 'сьогодні після 09:00', 'завтра після 09:00',
    'у понеділок після 10:00'."""
    local = now.astimezone(ZoneInfo(cfg.timezone))
    today = owner_ranges(cfg, local.date())
    if any(r.start <= local.time() < r.end for r in today):
        return eta_phrase("soon", lang)
    later = [r for r in today if r.start > local.time()]
    if later:
        return eta_phrase("today", lang, time=f"{later[0].start:%H:%M}")
    for offset in range(1, 15):
        day = local.date() + timedelta(days=offset)
        ranges = owner_ranges(cfg, day)
        if ranges:
            start = f"{ranges[0].start:%H:%M}"
            if offset == 1:
                return eta_phrase("tomorrow", lang, time=start)
            return eta_phrase("weekday", lang, time=start, day=day)
    return eta_phrase("soon", lang)


def start_handoff(conversation: Conversation, reason: str, now: datetime) -> None:
    conversation.state = "HANDOFF_PENDING"
    conversation.handoff_reason = reason if reason in HANDOFF_REASONS else "other"
    conversation.handoff_at = now
    conversation.handoff_reminded_at = None
    conversation.holding_sent_at = now  # the handoff reply counts as the holding message


def take_over(conversation: Conversation, now: datetime) -> None:
    conversation.state = "HUMAN_ACTIVE"
    conversation.last_human_message_at = now


def return_to_bot(conversation: Conversation) -> None:
    conversation.state = "BOT_ACTIVE"
    conversation.handoff_reason = None
    conversation.handoff_at = None
    conversation.handoff_reminded_at = None
