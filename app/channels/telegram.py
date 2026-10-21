"""Telegram outbound channels: the public demo bot, Telegram Business and the sandbox.

Owner Bot is one bot that masters talk to and also connect to their accounts as a business
bot; the demo bot is a separate public bot of the demo studio.
"""

from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import LinkPreviewOptions

from app.channels.base import ChannelError
from app.core.text import split_message
from app.db.models import ChannelAccount

TELEGRAM_MAX_CHARS = 4096
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def _send(
    bot: Bot, chat_id: int, parts: list[str], business_connection_id: str | None
) -> list[str]:
    ids = []
    for part in parts:
        try:
            message = await bot.send_message(
                chat_id,
                part,
                business_connection_id=business_connection_id,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramForbiddenError as exc:  # blocked the bot, or no right to reply
            raise ChannelError(f"forbidden: {exc.message}", permanent=True) from exc
        except TelegramRetryAfter as exc:
            raise ChannelError(f"rate limited for {exc.retry_after}s") from exc
        except TelegramBadRequest as exc:  # e.g. the 24-hour business window has passed
            raise ChannelError(f"bad request: {exc.message}", permanent=True) from exc
        except TelegramAPIError as exc:
            raise ChannelError(str(exc)) from exc
        ids.append(str(message.message_id))
    return ids


class TelegramDemoChannel:
    """The public demo bot: an ordinary bot, no reply window."""

    kind = "telegram_demo"
    max_bytes: int | None = None
    max_chars = TELEGRAM_MAX_CHARS
    window: timedelta | None = None

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_text(self, account: ChannelAccount, recipient_id: str, text: str) -> list[str]:
        return await _send(
            self.bot, int(recipient_id), split_message(text, max_chars=self.max_chars), None
        )


class TelegramBusinessChannel:
    """Replies on behalf of the master's own Telegram account (R11: 24-hour window)."""

    kind = "telegram_business"
    max_bytes: int | None = None
    max_chars = TELEGRAM_MAX_CHARS
    window = timedelta(hours=24)

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_text(self, account: ChannelAccount, recipient_id: str, text: str) -> list[str]:
        parts = split_message(text, max_chars=self.max_chars)
        return await _send(self.bot, int(recipient_id), parts, account.external_id)


class SandboxChannel:
    """The master tests the bot inside Owner Bot; replies come back to the master's chat."""

    kind = "sandbox"
    max_bytes: int | None = None
    max_chars = TELEGRAM_MAX_CHARS - 20
    window: timedelta | None = None

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_text(self, account: ChannelAccount, recipient_id: str, text: str) -> list[str]:
        parts = [f"🤖 {part}" for part in split_message(text, max_chars=self.max_chars)]
        return await _send(self.bot, int(recipient_id), parts, None)
