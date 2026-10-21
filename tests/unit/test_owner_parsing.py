from datetime import date

from app.owner.parsing import parse_day, parse_day_range, parse_local_datetime
from tests.helpers import kyiv

TODAY = date(2026, 10, 21)
NOW = kyiv("2026-10-21 12:00")


def test_parse_day() -> None:
    assert parse_day("25.10", TODAY) == date(2026, 10, 25)
    assert parse_day("завтра", TODAY) == date(2026, 10, 22)
    assert parse_day("сьогодні", TODAY) == TODAY
    assert parse_day("05.01", date(2026, 12, 28)) == date(2027, 1, 5)
    assert parse_day("31.02", TODAY) is None
    assert parse_day("колись", TODAY) is None


def test_parse_local_datetime() -> None:
    assert parse_local_datetime("22.10 15:30", NOW) == kyiv("2026-10-22 15:30")
    assert parse_local_datetime("завтра 9:00", NOW) == kyiv("2026-10-22 09:00")
    assert parse_local_datetime("15:30", NOW) == kyiv("2026-10-21 15:30")
    assert parse_local_datetime("10:00", NOW) == kyiv("2026-10-22 10:00")  # already past today
    assert parse_local_datetime("25.10", NOW) is None  # no time
    assert parse_local_datetime("25:61", NOW) is None


def test_parse_day_range() -> None:
    assert parse_day_range("25.10-27.10 відпустка", TODAY) == (
        date(2026, 10, 25),
        date(2026, 10, 27),
        "відпустка",
    )
    assert parse_day_range("завтра немає світла", TODAY) == (
        date(2026, 10, 22),
        date(2026, 10, 22),
        "немає світла",
    )
    assert parse_day_range("абв", TODAY) is None
