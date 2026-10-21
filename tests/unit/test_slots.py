from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.scheduling.slots import Interval, find_slots, local_to_utc, pick_spread
from tests.helpers import KYIV, demo_config, kyiv

STAFF = ["olena", "iryna"]


def local_times(slots, tz=KYIV) -> list[str]:
    return [s.start.astimezone(tz).strftime("%Y-%m-%d %H:%M") for s in slots]


def test_night_request_for_tomorrow_after_15() -> None:
    cfg = demo_config()
    now = kyiv("2026-10-20 23:47")  # Tuesday night, like the landing demo
    slots = find_slots(
        cfg,
        "gel_manicure",
        STAFF,
        date(2026, 10, 21),
        date(2026, 10, 21),
        now,
        {},
        time_from=time(15, 0),
    )
    # 120 min service, studio closes 20:00: starts 15:00..18:00, spread to three.
    assert local_times(slots) == ["2026-10-21 15:00", "2026-10-21 16:30", "2026-10-21 18:00"]
    assert all(s.end - s.start == timedelta(minutes=120) for s in slots)
    assert all(s.blocked_until - s.end == timedelta(minutes=15) for s in slots)


def test_busy_staff_and_buffer_are_respected() -> None:
    cfg = demo_config()
    now = kyiv("2026-10-20 23:47")
    busy = Interval(kyiv("2026-10-21 10:00"), kyiv("2026-10-21 17:15"))
    slots = find_slots(
        cfg,
        "gel_manicure",
        ["olena"],
        date(2026, 10, 21),
        date(2026, 10, 21),
        now,
        {"olena": [busy]},
    )
    # Free from 17:15; the 30-minute grid gives 17:30; 120 min must end by 20:00.
    assert local_times(slots) == ["2026-10-21 17:30", "2026-10-21 18:00"]


def test_second_master_covers_busy_first_master() -> None:
    cfg = demo_config()
    now = kyiv("2026-10-20 23:47")
    busy_day = Interval(kyiv("2026-10-21 00:00"), kyiv("2026-10-22 00:00"))
    slots = find_slots(
        cfg,
        "gel_manicure",
        STAFF,
        date(2026, 10, 21),
        date(2026, 10, 21),
        now,
        {"olena": [busy_day]},
    )
    assert slots and all(s.staff_key == "iryna" for s in slots)


def test_min_lead_time() -> None:
    cfg = demo_config()  # min_lead_minutes = 60
    now = kyiv("2026-10-21 14:10")
    slots = find_slots(
        cfg, "gel_removal", ["olena"], date(2026, 10, 21), date(2026, 10, 21), now, {}
    )
    assert local_times(slots)[0] == "2026-10-21 15:30"


def test_closed_days_exceptions_and_horizon() -> None:
    cfg = demo_config(booking={**demo_config().booking.model_dump(), "horizon_days": 120})
    now = kyiv("2026-10-21 09:00")
    sunday = date(2026, 10, 25)
    assert find_slots(cfg, "gel_manicure", STAFF, sunday, sunday, now, {}) == []
    new_year = date(2027, 1, 1)
    assert find_slots(cfg, "gel_manicure", STAFF, new_year, new_year, now, {}) == []
    short_day = date(2026, 12, 31)  # 10:00-15:00 exception
    slots = find_slots(
        cfg, "gel_manicure", STAFF, short_day, short_day, now, {}, per_day=20, limit=20
    )
    assert local_times(slots)[-1] == "2026-12-31 13:00"

    default_horizon = demo_config()  # 30 days
    far = date(2026, 12, 1)
    assert find_slots(default_horizon, "gel_manicure", STAFF, far, far, now, {}) == []


def test_limit_and_spread() -> None:
    assert pick_spread(list(range(7)), 3) == [0, 3, 6]
    assert pick_spread([1, 2], 3) == [1, 2]
    cfg = demo_config()
    now = kyiv("2026-10-21 09:00")
    slots = find_slots(cfg, "gel_removal", STAFF, date(2026, 10, 22), date(2026, 10, 30), now, {})
    assert len(slots) == cfg.booking.max_slots_returned
    assert len({s.start for s in slots}) == len(slots)  # one master per start time


def _all_day_config(tz: str):
    hours = {
        d: [{"start": "00:30", "end": "06:00"}]
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }
    return demo_config(timezone=tz, hours=hours, owner_hours=None, exceptions=[])


def test_local_to_utc_skips_nonexistent_time() -> None:
    warsaw = ZoneInfo("Europe/Warsaw")
    # EU clocks jump from 02:00 to 03:00 on 2027-03-28 in Warsaw.
    assert local_to_utc(date(2027, 3, 28), time(2, 30), warsaw) is None
    assert local_to_utc(date(2027, 3, 28), time(3, 30), warsaw) == datetime(
        2027, 3, 28, 1, 30, tzinfo=UTC
    )


def test_dst_spring_forward_day() -> None:
    cfg = _all_day_config("Europe/Warsaw")
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2027, 3, 1, 12, 0, tzinfo=UTC)
    day = date(2027, 3, 28)
    slots = find_slots(cfg, "manicure_no_coating", ["olena"], day, day, now, {}, per_day=50)
    starts = local_times(slots, tz)
    assert "2027-03-28 02:00" not in starts and "2027-03-28 02:30" not in starts
    for s in slots:
        assert s.end - s.start == timedelta(minutes=60)  # real minutes, not wall clock
        local = s.start.astimezone(tz)
        assert local.minute in (0, 30)


def test_dst_fall_back_day() -> None:
    cfg = _all_day_config("Europe/Warsaw")
    tz = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    day = date(2026, 10, 25)  # clocks go back from 03:00 to 02:00
    slots = find_slots(cfg, "manicure_no_coating", ["olena"], day, day, now, {}, per_day=50)
    starts = local_times(slots, tz)
    assert len(starts) == len(set(starts))  # the repeated hour is offered once
    assert all(s.end - s.start == timedelta(minutes=60) for s in slots)


def test_kyiv_dst_days_have_consistent_slots() -> None:
    cfg = _all_day_config("Europe/Kyiv")
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    for day in (date(2026, 10, 25), date(2027, 3, 28)):
        slots = find_slots(
            cfg.model_copy(
                update={"booking": cfg.booking.model_copy(update={"horizon_days": 180})}
            ),
            "manicure_no_coating",
            ["olena"],
            day,
            day,
            now,
            {},
            per_day=50,
        )
        assert slots
        for s in slots:
            local = s.start.astimezone(KYIV)
            assert local.date() == day
            assert local_to_utc(day, local.time().replace(tzinfo=None), KYIV) == s.start
            assert s.end - s.start == timedelta(minutes=60)
