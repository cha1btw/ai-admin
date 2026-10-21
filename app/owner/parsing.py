"""Parsing what masters type: dates and times in Ukrainian habits."""

import re
from datetime import date, datetime, time, timedelta

_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?")
_TIME = re.compile(r"(\d{1,2})[:.](\d{2})(?!\.\d)")
_RELATIVE = {"сьогодні": 0, "сегодня": 0, "завтра": 1, "післязавтра": 2, "послезавтра": 2}


def _year(raw: str | None, today: date) -> int:
    if raw is None:
        return today.year
    value = int(raw)
    return 2000 + value if value < 100 else value


def parse_day(text: str, today: date) -> date | None:
    lowered = text.strip().lower()
    for word, offset in _RELATIVE.items():
        if lowered.startswith(word):
            return today + timedelta(days=offset)
    match = _DATE.search(lowered)
    if not match:
        return None
    try:
        day = date(_year(match.group(3), today), int(match.group(2)), int(match.group(1)))
    except ValueError:
        return None
    if match.group(3) is None and day < today - timedelta(days=1):
        day = day.replace(year=day.year + 1)  # "05.01" typed in December means next January
    return day


def parse_local_datetime(text: str, now_local: datetime) -> datetime | None:
    """'22.10 15:30', 'завтра 15:30', '15:30' (today, or tomorrow if already past)."""
    lowered = text.strip().lower()
    time_match = _TIME.search(_DATE.sub(" ", lowered) if _DATE.search(lowered) else lowered)
    if not time_match:
        return None
    hour, minute = int(time_match.group(1)), int(time_match.group(2))
    if hour > 23 or minute > 59:
        return None
    at = time(hour, minute)
    today = now_local.date()
    day = parse_day(lowered, today)
    if day is None:
        day = today if at > now_local.time() else today + timedelta(days=1)
    return datetime.combine(day, at, tzinfo=now_local.tzinfo)


def parse_day_range(text: str, today: date) -> tuple[date, date, str] | None:
    """'25.10', '25.10-27.10', 'завтра відключення світла' -> (first, last, reason)."""
    lowered = text.strip()
    parts = re.split(r"\s*[-–—]\s*", lowered, maxsplit=1)
    first = parse_day(parts[0], today)
    if first is None:
        return None
    last = first
    if len(parts) == 2:
        second = parse_day(parts[1], today)
        if second is not None:
            last = second
    if last < first:
        first, last = last, first
    reason = _DATE.sub("", lowered)
    for word in _RELATIVE:
        reason = re.sub(rf"\b{word}\b", "", reason, flags=re.IGNORECASE)
    reason = reason.strip(" -–—,.")
    return first, last, reason
