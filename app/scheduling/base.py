"""Calendar provider interface. Our own bookings and closures are always read from the database;
a provider adds what only it knows (busy time in the master's Google Calendar) and mirrors
bookings into it."""

from typing import Protocol

from app.db.models import Booking, Staff
from app.scheduling.slots import Interval
from app.tenants.schema import StudioConfig


class CalendarError(Exception):
    """The external calendar failed; the booking must not be created."""


class CalendarProvider(Protocol):
    kind: str
    supports_slots: bool

    async def busy(self, cfg: StudioConfig, staff: Staff, window: Interval) -> list[Interval]: ...

    async def create_event(
        self, cfg: StudioConfig, staff: Staff, booking: Booking, title: str, description: str
    ) -> str | None: ...

    async def delete_event(self, cfg: StudioConfig, staff: Staff, event_id: str) -> None: ...


CalendarRegistry = dict[str, CalendarProvider]
