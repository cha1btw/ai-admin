"""How messages reach masters and founders. TelegramNotifier lives in owner/telegram.py so that
tests and the CLI work without aiogram network calls."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Button:
    text: str
    data: str  # Telegram callback data, at most 64 bytes


Keyboard = list[list[Button]]

# The always-visible keyboard under the input field. Pressing a button sends its text as an
# ordinary message, which app/owner/bot.py maps back to the action.
MENU_LABELS: list[list[str]] = [
    ["📅 Сьогодні", "📊 Статистика"],
    ["⚙️ Режим", "🧪 Пісочниця"],
    ["📋 Що знає бот", "✏️ Повідомити про зміни"],
    ["⏸ Зупинити бота", "▶️ Увімкнути бота"],
    ["📌 Стан", "❓ Допомога"],
]


class OwnerNotifier(Protocol):
    async def send(
        self, chat_id: int, text: str, *, buttons: Keyboard | None = None, silent: bool = False
    ) -> int | None: ...

    async def edit(
        self, chat_id: int, message_id: int, text: str, *, buttons: Keyboard | None = None
    ) -> None: ...

    async def send_photo(
        self, chat_id: int, photo: str, caption: str, *, silent: bool = False
    ) -> int | None: ...

    async def show_menu(self, chat_id: int, text: str) -> None:
        """Send `text` and pin the MENU_LABELS keyboard under the input field."""
        ...


@dataclass
class Sent:
    chat_id: int
    text: str
    buttons: Keyboard | None
    silent: bool
    message_id: int


@dataclass
class RecordingNotifier:
    """Keeps notifications in memory; prints them in `make chat`."""

    echo: bool = False
    sent: list[Sent] = field(default_factory=list)
    edits: list[tuple[int, int, str]] = field(default_factory=list)
    menu_shown: list[int] = field(default_factory=list)
    _next_id: int = 1000

    async def send(
        self, chat_id: int, text: str, *, buttons: Keyboard | None = None, silent: bool = False
    ) -> int | None:
        self._next_id += 1
        self.sent.append(Sent(chat_id, text, buttons, silent, self._next_id))
        if self.echo:
            labels = " ".join(f"[{b.text}]" for row in buttons or [] for b in row)
            print(f"\n[Майстру{', без звуку' if silent else ''}] {text}\n{labels}".rstrip())
        return self._next_id

    async def edit(
        self, chat_id: int, message_id: int, text: str, *, buttons: Keyboard | None = None
    ) -> None:
        self.edits.append((chat_id, message_id, text))

    async def send_photo(
        self, chat_id: int, photo: str, caption: str, *, silent: bool = False
    ) -> int | None:
        return await self.send(chat_id, f"[фото] {caption}", silent=silent)

    async def show_menu(self, chat_id: int, text: str) -> None:
        self.menu_shown.append(chat_id)
        await self.send(chat_id, text)

    def texts(self) -> list[str]:
        return [s.text for s in self.sent]
