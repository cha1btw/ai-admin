"""Things the master should hear about. Produced inside a transaction, delivered after commit
(app/owner/notifications.py), so a rolled-back booking never produces a notification."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BookingCreated:
    tenant_id: int
    booking_id: int
    message_id: int | None = None


@dataclass(frozen=True)
class BookingCancelled:
    tenant_id: int
    booking_id: int


@dataclass(frozen=True)
class BookingRescheduled:
    tenant_id: int
    old_booking_id: int
    new_booking_id: int


@dataclass(frozen=True)
class RequestCreated:
    tenant_id: int
    booking_id: int


@dataclass(frozen=True)
class HandoffStarted:
    tenant_id: int
    conversation_id: int
    reason: str
    summary: str


@dataclass(frozen=True)
class DraftCreated:
    tenant_id: int
    draft_id: int


@dataclass(frozen=True)
class MediaReceived:
    tenant_id: int
    conversation_id: int
    message_id: int


@dataclass(frozen=True)
class SendFailed:
    tenant_id: int
    conversation_id: int
    text: str
    reason: str


Event = (
    BookingCreated
    | BookingCancelled
    | BookingRescheduled
    | RequestCreated
    | HandoffStarted
    | DraftCreated
    | MediaReceived
    | SendFailed
)
