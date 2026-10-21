"""First-run setup for founders: `make setup`.

Asks for secrets with hidden input and writes them to .env, so keys never pass through chat,
shell history or logs. Then checks them: the bot token with getMe (and Business Mode), the model
key with one tiny request, and learns the founder's Telegram id from a message to the bot.
"""

import asyncio
import getpass
import os
import re
import secrets
import shutil
import time
from pathlib import Path

from cryptography.fernet import Fernet

ENV = Path(".env")
EXAMPLE = Path(".env.example")
ID_WAIT_S = 180


def env_value(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}=(.*)$", text, re.M)
    return match.group(1).strip() if match else ""


def set_env_values(text: str, values: dict[str, str]) -> str:
    """Replace KEY=... lines (or append them) and leave every other line as it was."""
    for key, value in values.items():
        line = f"{key}={value}"
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
        if pattern.search(text):
            text = pattern.sub(lambda _, line=line: line, text, count=1)
        else:
            text = text.rstrip("\n") + "\n" + line + "\n"
    return text


PASTE_MARKERS = re.compile(r"\x1b\[20[01]~")


def clean_secret(raw: str) -> str:
    """Drops what terminals and messengers add around a pasted key: bracketed-paste markers,
    control characters, surrounding spaces and quotes."""
    text = PASTE_MARKERS.sub("", raw)
    text = "".join(c for c in text if c >= " " or c == "\t")
    return text.strip().strip("'\"«»“”").strip()


def describe(secret: str) -> str:
    """What was received, without showing the secret: length, first characters, warnings."""
    note = f"отримано {len(secret)} символів, починається з «{secret[:4]}»"
    if any(c.isspace() for c in secret):
        note += ". Є пробіли: схоже, вставлено не той текст"
    return note


def ask_secret(label: str, current: str) -> str | None:
    hint = " (Enter: залишити поточний)" if current else " (Enter: пропустити)"
    value = clean_secret(getpass.getpass(f"{label}{hint}: "))
    if value:
        print(f"  {describe(value)}")
    return value or None


async def check_bot(token: str) -> str | None:
    """Bot username if Telegram accepts the token."""
    from aiogram import Bot

    try:
        bot = Bot(token=token)
    except Exception:
        print(
            "✗ Це не схоже на токен бота. Він виглядає як 1234567890:AAH… "
            "(цифри, двокрапка, ще ~35 символів). Скопіюйте його з @BotFather повністю."
        )
        return None
    try:
        me = await bot.get_me()
    except Exception as exc:
        print(f"✗ Telegram не прийняв токен ({type(exc).__name__}).")
        return None
    finally:
        await bot.session.close()
    print(f"✓ Бот @{me.username}")
    if not me.can_connect_to_business:
        print(
            "! Business Mode вимкнено: @BotFather → /mybots → бот → Bot Settings → "
            "Business Mode → Turn on. Без цього бота не підключити до Telegram для бізнесу."
        )
    return me.username


async def check_model(model: str, base_url: str | None, key: str) -> bool:
    from app.llm.base import ChatMessage, LLMError
    from app.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(
        api_key=key, base_url=base_url, model=model, timeout_s=30, max_completion_tokens=200
    )
    try:
        await provider.complete([ChatMessage("user", "Відповідай одним словом: працюєш?")], [])
    except LLMError as exc:
        print(f"✗ Модель {model} не відповіла: {str(exc)[:200]}")
        return False
    print(f"✓ Модель {model} відповідає")
    return True


async def learn_admin_id(token: str) -> int | None:
    """The id of whoever writes to the bot first within ID_WAIT_S."""
    from aiogram import Bot

    bot = Bot(token=token)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        deadline = time.monotonic() + ID_WAIT_S
        offset = None
        while time.monotonic() < deadline:
            updates = await bot.get_updates(offset=offset, timeout=20, allowed_updates=["message"])
            for update in updates:
                offset = update.update_id + 1
                if update.message and update.message.from_user:
                    user = update.message.from_user
                    await bot.get_updates(offset=offset, timeout=0)  # mark it read
                    await bot.send_message(user.id, f"✓ Ваш id {user.id} записано в налаштування.")
                    return user.id
        return None
    finally:
        await bot.session.close()


def run() -> int:
    if not ENV.exists():
        shutil.copy(EXAMPLE, ENV)
        print("Створено .env з .env.example")
    text = ENV.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    if not env_value(text, "ENCRYPTION_KEY"):
        values["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    if not env_value(text, "TELEGRAM_WEBHOOK_SECRET"):
        values["TELEGRAM_WEBHOOK_SECRET"] = secrets.token_urlsafe(32)

    print("Секрети вводяться приховано: символи не видно, це нормально.\n")
    token = username = None
    for _ in range(3):
        current = env_value(text, "OWNER_BOT_TOKEN")
        token = ask_secret("Токен Owner Bot з @BotFather", current) or current
        if not token:
            break
        username = asyncio.run(check_bot(token))
        if username:
            values["OWNER_BOT_TOKEN"] = token
            break

    model = env_value(text, "LLM_MODEL")
    base_url = env_value(text, "LLM_BASE_URL") or None
    key_name = "LLM_API_KEY" if base_url else "OPENAI_API_KEY"
    print(f"\nМодель: {model or '—'}, ключ іде в {key_name}")
    for _ in range(3):
        current = env_value(text, key_name)
        key = ask_secret("Ключ моделі (Google AI Studio → Create API key)", current) or current
        if not key:
            break
        if not model:
            print("! LLM_MODEL порожній у .env, модель не перевірено.")
            values[key_name] = key
            break
        # A key the model rejects is not saved: a broken key would only fail later in silence.
        if asyncio.run(check_model(model, base_url, key)):
            values[key_name] = key
            break

    if username and not env_value(text, "ADMIN_TELEGRAM_IDS"):
        print(f"\nНапишіть боту @{username} будь-яке повідомлення з вашого акаунта (чекаю 3 хв)…")
        admin_id = asyncio.run(learn_admin_id(token))
        if admin_id is None:
            typed = input("Не дочекався. Введіть ваш Telegram id вручну (Enter: пропустити): ")
            admin_id = int(typed) if typed.strip().isdigit() else None
        if admin_id is not None:
            print(f"✓ Ваш Telegram id: {admin_id}")
            values["ADMIN_TELEGRAM_IDS"] = str(admin_id)
            if not env_value(text, "ALERT_CHAT_ID"):
                values["ALERT_CHAT_ID"] = str(admin_id)

    ENV.write_text(set_env_values(text, values), encoding="utf-8")
    os.chmod(ENV, 0o600)  # secrets: readable by you only
    print(f"\nЗаписано в .env: {', '.join(values) or 'нічого нового'}")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        print("\nСкасовано, .env не змінено.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
