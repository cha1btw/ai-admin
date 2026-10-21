"""Outbound side of a channel. Inbound parsing lives next to each channel's webhook handler and
produces app.brain.ingest.Inbound, so the brain never knows where a message came from."""

from datetime import datetime, timedelta
from typing import Protocol

from app.db.models import ChannelAccount, Conversation


class ChannelError(Exception):
    """Sending failed. permanent=True means retrying will not help (blocked bot, closed window)."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class OutboundChannel(Protocol):
    kind: str
    max_bytes: int | None
    max_chars: int | None
    window: timedelta | None  # None: the channel lets us write any time

    async def send_text(self, account: ChannelAccount, recipient_id: str, text: str) -> list[str]:
        """Send one or more parts; return the external message ids."""
        ...


ChannelRegistry = dict[str, OutboundChannel]


def window_open(channel: OutboundChannel, conversation: Conversation, now: datetime) -> bool:
    """R11: Telegram Business and Instagram allow replies only within 24 hours of the client's
    last message."""
    if channel.window is None:
        return True
    last = conversation.last_client_message_at
    return last is not None and now - last < channel.window
