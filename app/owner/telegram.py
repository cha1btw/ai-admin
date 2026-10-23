"""OwnerNotifier on top of the Owner Bot (aiogram)."""

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
)

from app.core.logging import get_logger
from app.owner.notifier import MENU_LABELS, Keyboard

log = get_logger(__name__)


def markup(buttons: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row]
            for row in buttons
        ]
    )


def menu_markup() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in MENU_LABELS],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть кнопку або напишіть команду",
    )


class TelegramNotifier:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(
        self, chat_id: int, text: str, *, buttons: Keyboard | None = None, silent: bool = False
    ) -> int | None:
        try:
            message = await self.bot.send_message(
                chat_id,
                text[:4096],
                reply_markup=markup(buttons),
                disable_notification=silent,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            log.warning("owner.send_failed", chat_id=chat_id, error=type(exc).__name__)
            return None
        return message.message_id

    async def show_menu(self, chat_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id, text, reply_markup=menu_markup())
        except TelegramAPIError as exc:
            log.warning("owner.menu_failed", chat_id=chat_id, error=type(exc).__name__)

    async def edit(
        self, chat_id: int, message_id: int, text: str, *, buttons: Keyboard | None = None
    ) -> None:
        try:
            await self.bot.edit_message_text(
                text=text[:4096],
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=markup(buttons),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            log.warning("owner.edit_failed", chat_id=chat_id, error=type(exc).__name__)

    async def send_photo(
        self, chat_id: int, photo: str, caption: str, *, silent: bool = False
    ) -> int | None:
        try:
            message = await self.bot.send_photo(
                chat_id, photo, caption=caption[:1024], disable_notification=silent
            )
        except TelegramAPIError as exc:
            log.warning("owner.photo_failed", chat_id=chat_id, error=type(exc).__name__)
            return await self.send(chat_id, caption, silent=silent)
        return message.message_id
