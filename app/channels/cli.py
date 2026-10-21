"""Terminal channel for `make chat` and tests: messages go to a list and optionally stdout."""

import itertools
from datetime import timedelta

from app.db.models import ChannelAccount

_ids = itertools.count(1)


class CLIChannel:
    kind = "cli"
    max_bytes: int | None = None
    max_chars: int | None = None
    window: timedelta | None = None

    def __init__(self, *, echo: bool = False) -> None:
        self.echo = echo
        self.sent: list[tuple[str, str]] = []  # (recipient_id, text)

    async def send_text(self, account: ChannelAccount, recipient_id: str, text: str) -> list[str]:
        self.sent.append((recipient_id, text))
        if self.echo:
            print(f"\nБот: {text}\n")
        return [f"cli-{next(_ids)}"]

    def texts(self) -> list[str]:
        return [text for _, text in self.sent]
