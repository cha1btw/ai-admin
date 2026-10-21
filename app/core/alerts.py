"""Alerts to founders in Telegram, with simple de-duplication so one failure is one message."""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.core.clock import Clock
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.owner.notifier import OwnerNotifier

log = get_logger(__name__)


class Alerts:
    def __init__(
        self,
        notifier: "OwnerNotifier | None",
        chat_id: int | None,
        clock: Clock,
        *,
        dedupe: timedelta = timedelta(minutes=10),
    ) -> None:
        self._notifier = notifier
        self._chat_id = chat_id
        self._clock = clock
        self._dedupe = dedupe
        self._last: dict[str, datetime] = {}
        self.sent: list[str] = []

    async def send(self, key: str, text: str) -> None:
        """key groups repeats, e.g. 'llm_down' or 'instagram_token:12'.
        Never put client data into the text."""
        now = self._clock.now()
        last = self._last.get(key)
        if last is not None and now - last < self._dedupe:
            return
        self._last[key] = now
        self.sent.append(text)
        log.error("alert", key=key)
        if self._notifier is not None and self._chat_id is not None:
            try:
                await self._notifier.send(self._chat_id, f"⚠️ {text}")
            except Exception:  # an alert must never break the caller
                log.exception("alert.send_failed", key=key)
