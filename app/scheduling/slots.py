"""Slot math: studio hours plus busy intervals give free start times. Pure functions, no I/O.

Times are generated on the local wall clock of the studio (10:00, 10:30, ...) and converted to
UTC one by one, so daylight saving transitions are handled by zoneinfo, not by hand-written
offsets. Durations are added in UTC (real minutes).
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.tenants.schema import ServiceConfig, StudioConfig, TimeRange


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class SlotOption:
    staff_key: str
    start: datetime  # UTC
    end: datetime  # UTC, end of the service
    blocked_until: datetime  # end plus buffer


def local_to_utc(day: date, at: time, tz: ZoneInfo) -> datetime | None:
    """Local wall time to UTC; None if that wall time does not exist (spring-forward gap)."""
    local = datetime.combine(day, at, tzinfo=tz)
    utc = local.astimezone(UTC)
    if utc.astimezone(tz).replace(tzinfo=None) != local.replace(tzinfo=None):
        return None
    return utc


def day_ranges(cfg: StudioConfig, day: date) -> list[TimeRange]:
    exception = cfg.exception_for(day)
    if exception is not None:
        return [] if exception.closed else list(exception.hours or [])
    return cfg.hours.for_weekday(day.weekday())


def _range_interval(day: date, rng: TimeRange, tz: ZoneInfo) -> Interval | None:
    start = local_to_utc(day, rng.start, tz)
    end = local_to_utc(day, rng.end, tz)
    if start is None or end is None or start >= end:
        return None
    return Interval(start, end)


def _wall_times(start: time, end: time, step_min: int) -> Iterator[time]:
    anchor = date(2000, 1, 1)
    current = datetime.combine(anchor, start)
    stop = datetime.combine(anchor, end)
    while current < stop:
        yield current.time()
        current += timedelta(minutes=step_min)


def day_candidates(
    cfg: StudioConfig,
    service: ServiceConfig,
    day: date,
    busy: list[Interval],
    earliest: datetime,
) -> list[SlotOption]:
    """All free starts for one staff member on one local day (staff_key left empty)."""
    tz = ZoneInfo(cfg.timezone)
    ranges = day_ranges(cfg, day)
    if not ranges:
        return []
    breaks = [iv for b in cfg.breaks if (iv := _range_interval(day, b, tz)) is not None]
    duration = timedelta(minutes=service.duration_min)
    buffer = timedelta(minutes=service.buffer_min)
    result: list[SlotOption] = []
    for rng in ranges:
        window = _range_interval(day, rng, tz)
        if window is None:
            continue
        for wall in _wall_times(rng.start, rng.end, cfg.booking.slot_step_minutes):
            start = local_to_utc(day, wall, tz)
            if start is None or start < earliest:
                continue
            end = start + duration
            if end > window.end:
                break
            if any(Interval(start, end).overlaps(b) for b in breaks):
                continue
            held = Interval(start, end + buffer)
            if any(held.overlaps(b) for b in busy):
                continue
            result.append(SlotOption("", start, end, end + buffer))
    return result


def pick_spread[T](items: list[T], k: int) -> list[T]:
    """Up to k items spread over the list: first, middle, last rather than three in a row."""
    if len(items) <= k:
        return items
    if k == 1:
        return items[:1]
    indexes = sorted({round(i * (len(items) - 1) / (k - 1)) for i in range(k)})
    return [items[i] for i in indexes]


def find_slots(
    cfg: StudioConfig,
    service_key: str,
    staff_keys: list[str],
    date_from: date,
    date_to: date,
    now: datetime,
    busy_by_staff: dict[str, list[Interval]],
    *,
    time_from: time | None = None,
    time_to: time | None = None,
    limit: int | None = None,
    per_day: int = 3,
) -> list[SlotOption]:
    """Free slots for a service between two local dates (inclusive), nearest first.

    time_from/time_to filter the local start time (time_to is the latest acceptable start).
    The same start time offered by two masters is shown once (the first master in order).
    """
    service = cfg.service(service_key)
    if service is None:
        raise ValueError(f"unknown service {service_key}")
    tz = ZoneInfo(cfg.timezone)
    limit = limit or cfg.booking.max_slots_returned
    earliest = now + timedelta(minutes=cfg.booking.min_lead_minutes)
    today = now.astimezone(tz).date()
    horizon = (now + timedelta(days=cfg.booking.horizon_days)).astimezone(tz).date()

    day = max(date_from, today)
    last = min(date_to, horizon)
    found: list[SlotOption] = []
    while day <= last and len(found) < limit:
        by_start: dict[datetime, SlotOption] = {}
        for staff_key in staff_keys:
            for option in day_candidates(
                cfg, service, day, busy_by_staff.get(staff_key, []), earliest
            ):
                local_start = option.start.astimezone(tz).time()
                if time_from is not None and local_start < time_from:
                    continue
                if time_to is not None and local_start > time_to:
                    continue
                by_start.setdefault(
                    option.start,
                    SlotOption(staff_key, option.start, option.end, option.blocked_until),
                )
        day_slots = [by_start[key] for key in sorted(by_start)]
        found.extend(pick_spread(day_slots, per_day))
        day += timedelta(days=1)
    return found[:limit]


def horizon_date(cfg: StudioConfig, now: datetime) -> date:
    tz = ZoneInfo(cfg.timezone)
    return (now + timedelta(days=cfg.booking.horizon_days)).astimezone(tz).date()
