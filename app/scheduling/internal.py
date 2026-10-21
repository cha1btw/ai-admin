"""Internal calendar: studio hours from the config plus bookings and /close blocks in our DB.

Used by the demo studio, the sandbox, tests and studios without any calendar of their own.
"""

from app.db.models import Booking, Staff
from app.scheduling.slots import Interval
from app.tenants.schema import StudioConfig


class InternalCalendar:
    kind = "internal"
    supports_slots = True

    async def busy(self, cfg: StudioConfig, staff: Staff, window: Interval) -> list[Interval]:
        return []  # everything lives in our database

    async def create_event(
        self, cfg: StudioConfig, staff: Staff, booking: Booking, title: str, description: str
    ) -> str | None:
        return None

    async def delete_event(self, cfg: StudioConfig, staff: Staff, event_id: str) -> None:
        return None
