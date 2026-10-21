"""Time source. All business logic takes time from a Clock so tests can freeze it."""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test clock: returns a fixed moment until moved explicitly."""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FrozenClock needs an aware datetime")
        self._now = moment.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment.astimezone(UTC)

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)
