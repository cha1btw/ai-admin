"""Owner Bot routers (aiogram). Thin: they translate Telegram updates into calls to
app.owner.actions and app.channels.telegram_inbound. The same bot receives Telegram Business
updates for masters who connected it to their accounts."""

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, CallbackQuery, Message
from sqlalchemy import select

from app.channels import telegram_inbound as inbound
from app.context import AppContext
from app.db.models import OwnerAccount, Tenant, TenantMember
from app.owner import actions
from app.owner.actions import Result
from app.owner.notifier import Button

MENU_HINT = "👇 Кнопки меню з'явилися під полем вводу."


async def send_result(app: AppContext, chat_id: int, result: Result | None) -> None:
    if result is None:
        return
    if result.text:
        await app.notifier.send(chat_id, result.text, buttons=result.buttons)
    for chat, message_id, text in result.edits:
        await app.notifier.edit(chat, message_id, text)


async def answer_button(app: AppContext, query: CallbackQuery, result: Result | None) -> None:
    """Show a button's result where the master pressed it: no pile of new messages and no
    stale buttons that could be pressed twice."""
    await query.answer(result.toast if result else None)
    if result is None:
        return
    message = query.message
    if message is None:
        return
    chat_id, message_id = message.chat.id, message.message_id
    if result.replace and result.text:
        await app.notifier.edit(chat_id, message_id, result.text, buttons=result.buttons)
    elif result.note:
        original = getattr(message, "text", None) or ""
        await app.notifier.edit(
            chat_id, message_id, f"{original}\n\n{result.note}", buttons=result.buttons
        )
        # The pressed message may be one of several copies of a draft; edits cover the rest.
        for chat, other_id, text in result.edits:
            await app.notifier.edit(chat, other_id, text)
    else:
        await send_result(app, chat_id, result)


def _name(message: Message) -> str | None:
    user = message.from_user
    if user is None:
        return None
    return " ".join(p for p in (user.first_name, user.last_name) if p) or user.username


# --- Telegram Business ------------------------------------------------------------------------


async def on_connection(connection: BusinessConnection, app: AppContext) -> None:
    await inbound.on_business_connection(app, connection)


async def on_business_message(message: Message, bot: Bot, app: AppContext) -> None:
    await inbound.on_business_message(app, bot, message)


async def on_business_edit(message: Message, bot: Bot, app: AppContext) -> None:
    await inbound.on_business_edit(app, bot, message)


async def on_business_deleted(event: BusinessMessagesDeleted, bot: Bot, app: AppContext) -> None:
    await inbound.on_business_deleted(app, bot, event)


# --- commands ----------------------------------------------------------------------------


async def start_with_token(message: Message, command: CommandObject, app: AppContext) -> None:
    result = await actions.link_account(
        app, command.args or "", message.from_user.id, _name(message)
    )
    if result and result.show_menu:
        await app.notifier.show_menu(message.chat.id, MENU_HINT)
    await send_result(app, message.chat.id, result)


async def start(message: Message, app: AppContext) -> None:
    user = message.from_user.id
    async with app.session_factory() as session, session.begin():
        tenant = await actions.current_tenant(session, app, user)
    if tenant is None:
        await help_command(message, app)
        return
    await app.notifier.show_menu(message.chat.id, MENU_HINT)
    await send_result(app, message.chat.id, await actions.home(app, user))


async def menu(message: Message, app: AppContext) -> None:
    await send_result(app, message.chat.id, await actions.home(app, message.from_user.id))


async def help_command(message: Message, app: AppContext) -> None:
    user = message.from_user.id
    async with app.session_factory() as session, session.begin():
        tenant = await actions.current_tenant(session, app, user)
    if tenant is None and not actions.is_admin(app, user):
        # The id helps setup: founders put it into ADMIN_TELEGRAM_IDS.
        await app.notifier.send(message.chat.id, f"{actions.NOT_LINKED}\n\nВаш Telegram id: {user}")
        return
    text = (f"Студія: {tenant.name}\n\n" if tenant else "") + actions.HELP
    if actions.is_admin(app, user):
        text += "\n\n" + actions.ADMIN_HELP
    await app.notifier.send(message.chat.id, text)


async def mode(message: Message, app: AppContext) -> None:
    await send_result(app, message.chat.id, await actions.show_mode(app, message.from_user.id))


async def pause(message: Message, app: AppContext) -> None:
    await send_result(app, message.chat.id, await actions.pause(app, message.from_user.id))


async def resume(message: Message, app: AppContext) -> None:
    await send_result(app, message.chat.id, await actions.resume(app, message.from_user.id))


async def stats(message: Message, command: CommandObject, app: AppContext) -> None:
    days = int(command.args) if command.args and command.args.strip().isdigit() else 7
    await send_result(
        app, message.chat.id, await actions.show_stats(app, message.from_user.id, days)
    )


async def change(message: Message, command: CommandObject, app: AppContext) -> None:
    result = await actions.start_change(app, message.from_user.id, (command.args or "").strip())
    await send_result(app, message.chat.id, result)


async def data(message: Message, app: AppContext) -> None:
    await send_result(app, message.chat.id, await actions.show_data(app, message.from_user.id))


async def sandbox(message: Message, app: AppContext) -> None:
    await send_result(app, message.chat.id, await actions.start_sandbox(app, message.from_user.id))


async def stop(message: Message, app: AppContext) -> None:
    await send_result(app, message.chat.id, await actions.stop_sandbox(app, message.from_user.id))


async def today(message: Message, app: AppContext) -> None:
    await send_result(app, message.chat.id, await actions.today_text(app, message.from_user.id))


async def close(message: Message, command: CommandObject, app: AppContext) -> None:
    result = await actions.close_days(app, message.from_user.id, command.args or "")
    await send_result(app, message.chat.id, result)


async def reopen(message: Message, command: CommandObject, app: AppContext) -> None:
    result = await actions.close_days(app, message.from_user.id, command.args or "", reopen=True)
    await send_result(app, message.chat.id, result)


async def cancel(message: Message, app: AppContext) -> None:
    await send_result(app, message.chat.id, await actions.cancel_pending(app, message.from_user.id))


async def skip(message: Message, app: AppContext) -> None:
    await send_result(
        app, message.chat.id, await actions.handle_text(app, message.from_user.id, "/skip")
    )


async def switch(message: Message, app: AppContext) -> None:
    async with app.session_factory() as session:
        rows = (
            await session.execute(
                select(Tenant.id, Tenant.name)
                .join(TenantMember, TenantMember.tenant_id == Tenant.id)
                .where(TenantMember.telegram_user_id == message.from_user.id)
                .order_by(Tenant.name)
            )
        ).all()
    if len(rows) < 2:
        await app.notifier.send(message.chat.id, "У вас одна студія.")
        return
    buttons = [[Button(name, f"sw:{tenant_id}")] for tenant_id, name in rows]
    await app.notifier.send(message.chat.id, "Оберіть студію:", buttons=buttons)


# --- founders' commands ----------------------------------------------------------------------


def _admin(message: Message, app: AppContext) -> bool:
    return message.from_user is not None and actions.is_admin(app, message.from_user.id)


async def tenants(message: Message, app: AppContext) -> None:
    if _admin(message, app):
        await app.notifier.send(message.chat.id, await actions.tenants_text(app))


async def health(message: Message, app: AppContext) -> None:
    if _admin(message, app):
        await app.notifier.send(message.chat.id, await actions.health_text(app))


async def link(message: Message, command: CommandObject, bot: Bot, app: AppContext) -> None:
    if not _admin(message, app):
        return
    parts = (command.args or "").split()
    if not parts:
        await app.notifier.send(
            message.chat.id, "Приклад: /link demo_studio або /link demo_studio admin"
        )
        return
    role = "admin" if len(parts) > 1 and parts[1] == "admin" else "owner"
    token = await actions.create_link(app, parts[0], role)
    if token is None:
        await app.notifier.send(message.chat.id, f"Студії {parts[0]} немає.")
        return
    me = await bot.me()
    await app.notifier.send(
        message.chat.id,
        f"Посилання для студії {parts[0]} ({role}), діє 3 дні, одноразове:\n"
        f"https://t.me/{me.username}?start={token}",
    )


async def admin_panel(message: Message, app: AppContext) -> None:
    if not _admin(message, app):
        return
    from app.admin.auth import login_link

    if not app.settings.public_base_url:
        await app.notifier.send(
            message.chat.id,
            "Задайте PUBLIC_BASE_URL у .env. Локально: uv run python -m app.cli admin link",
        )
        return
    await app.notifier.send(
        message.chat.id,
        "Вхід в адмінку, посилання одноразове і діє 10 хвилин:\n"
        + login_link(app.settings, message.from_user.id),
    )


async def pause_all(message: Message, app: AppContext) -> None:
    if _admin(message, app):
        await send_result(
            app, message.chat.id, await actions.set_all_status(app, message.from_user.id, "paused")
        )


async def resume_all(message: Message, app: AppContext) -> None:
    if _admin(message, app):
        await send_result(
            app, message.chat.id, await actions.set_all_status(app, message.from_user.id, "active")
        )


async def forget(message: Message, command: CommandObject, app: AppContext) -> None:
    if not _admin(message, app):
        return
    from app.privacy.deletion import forget_client

    arg = (command.args or "").strip()
    if not arg.isdigit():
        await app.notifier.send(message.chat.id, "Приклад: /forget 123 (id клієнтки з бази)")
        return
    done = await forget_client(app, int(arg), actor=f"admin:{message.from_user.id}")
    await app.notifier.send(message.chat.id, "Дані видалено." if done else "Клієнтку не знайдено.")


# --- buttons -----------------------------------------------------------------------------


async def on_button(query: CallbackQuery, app: AppContext) -> None:
    data, user = query.data or "", query.from_user.id
    kind, _, rest = data.partition(":")
    action, _, ident = rest.partition(":")
    result: Result | None = None
    try:
        if kind == "menu":  # inline main menu of an older message: it lives under the input now
            await query.answer("Меню тепер під полем вводу. Якщо його немає, натисніть /start")
            return
        if kind == "sb":
            result = await _sandbox_button(app, user, rest)
        elif kind == "d":
            handler = {
                "send": actions.approve_draft,
                "edit": actions.start_edit_draft,
                "no": actions.reject_draft,
            }[action]
            result = await handler(app, int(ident), user)
        elif kind == "c":
            handler = {
                "reply": actions.start_reply,
                "take": actions.take_conversation,
                "bot": actions.return_bot,
            }[action]
            result = await handler(app, int(ident), user)
        elif kind == "b":
            handler = {
                "cancel": actions.ask_cancel_booking,
                "cx": actions.cancel_booking_by_studio,
                "keep": actions.keep_booking,
                "paid": actions.confirm_payment,
            }[action]
            result = await handler(app, int(ident), user)
        elif kind == "r":
            handler = {
                "ok": actions.start_confirm_request,
                "other": actions.other_time,
                "no": actions.reject_request,
            }[action]
            result = await handler(app, int(ident), user)
        elif kind == "fb":
            result = await actions.feedback(app, int(rest), user)
        elif kind == "m":
            result = await actions.set_mode(app, user, rest)
        elif kind == "sw":
            result = await _switch(app, user, int(rest))
        else:
            raise KeyError(kind)
    except (KeyError, ValueError):
        await query.answer("Невідома дія")
        return
    await answer_button(app, query, result)


async def _sandbox_button(app: AppContext, user: int, action: str) -> Result:
    if action == "restart":
        return await actions.start_sandbox(app, user, restart=True)
    if action == "stop":
        return await actions.stop_sandbox(app, user)
    raise KeyError(action)


async def _switch(app: AppContext, user: int, tenant_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        if not await actions.can_manage(session, app, user, tenant_id):
            return Result("Немає доступу до цієї студії.")
        account = await actions.ensure_owner(session, user, None)
        account.active_tenant_id = tenant_id
        tenant = await session.get(Tenant, tenant_id)
        return Result(f"Активна студія: {tenant.name}.")


# --- plain messages ------------------------------------------------------------------------


async def stats_week(message: Message, app: AppContext) -> None:
    await send_result(app, message.chat.id, await actions.show_stats(app, message.from_user.id, 7))


async def change_prompt(message: Message, app: AppContext) -> None:
    result = await actions.start_change(app, message.from_user.id, None)
    await send_result(app, message.chat.id, result)


# Texts of the keyboard under the input field (MENU_LABELS) and what they do. They win over
# the sandbox, so pressing one never sends it to the bot as a client's message.
MENU_BUTTONS = {
    "📅 Сьогодні": today,
    "📊 Статистика": stats_week,
    "⚙️ Режим": mode,
    "🧪 Пісочниця": sandbox,
    "📋 Що знає бот": data,
    "✏️ Повідомити про зміни": change_prompt,
    "⏸ Зупинити бота": pause,
    "▶️ Увімкнути бота": resume,
    "📌 Стан": menu,
    "❓ Допомога": help_command,
    "🏠 Меню": menu,  # label of the first keyboard version, still on masters' screens
}


async def on_menu_button(message: Message, app: AppContext) -> None:
    await MENU_BUTTONS[message.text](message, app)


async def on_text(message: Message, app: AppContext) -> None:
    user = message.from_user.id
    async with app.session_factory() as session:
        account = await session.get(OwnerAccount, user)
        state = dict(account.state or {}) if account else {}
    if state.get("sandbox_tenant") and not state.get("awaiting"):
        sandbox_inbound = inbound.to_inbound(message)
        await actions.sandbox_message(app, user, state["sandbox_tenant"], sandbox_inbound)
        return
    if not message.text:
        await app.notifier.send(message.chat.id, "Надішліть, будь ласка, текст.")
        return
    await send_result(app, message.chat.id, await actions.handle_text(app, user, message.text))


# The "/" menu masters see in Owner Bot (set by `python -m app.cli telegram set-webhooks`).
# Founders' commands stay out of it: they are in /help for founders only.
MENU = [
    ("menu", "Стан студії"),
    ("today", "Записи на сьогодні"),
    ("mode", "Режим роботи бота"),
    ("pause", "Зупинити бота"),
    ("resume", "Увімкнути бота"),
    ("stats", "Статистика"),
    ("data", "Що знає бот"),
    ("change", "Повідомити про зміни цін чи графіка"),
    ("close", "Закрити дні (відпустка)"),
    ("sandbox", "Спробувати бота як клієнтка"),
    ("help", "Усі команди"),
]


def build_owner_dispatcher() -> Dispatcher:
    """Fresh routers per dispatcher: aiogram routers can belong to one dispatcher only."""
    business_router = Router(name="business")
    commands = Router(name="owner_commands")
    commands.message.filter(F.chat.type == "private")
    callbacks = Router(name="owner_callbacks")
    texts = Router(name="owner_texts")
    texts.message.filter(F.chat.type == "private")
    business_router.business_connection.register(on_connection)
    business_router.business_message.register(on_business_message)
    business_router.edited_business_message.register(on_business_edit)
    business_router.deleted_business_messages.register(on_business_deleted)
    commands.message.register(start_with_token, CommandStart(deep_link=True))
    commands.message.register(start, CommandStart())
    commands.message.register(help_command, Command("help"))
    commands.message.register(menu, Command("menu"))
    commands.message.register(mode, Command("mode"))
    commands.message.register(pause, Command("pause"))
    commands.message.register(resume, Command("resume"))
    commands.message.register(stats, Command("stats"))
    commands.message.register(data, Command("data"))
    commands.message.register(change, Command("change"))
    commands.message.register(sandbox, Command("sandbox"))
    commands.message.register(stop, Command("stop"))
    commands.message.register(today, Command("today"))
    commands.message.register(close, Command("close"))
    commands.message.register(reopen, Command("open"))
    commands.message.register(cancel, Command("cancel"))
    commands.message.register(skip, Command("skip"))
    commands.message.register(switch, Command("switch"))
    commands.message.register(tenants, Command("tenants"))
    commands.message.register(health, Command("health"))
    commands.message.register(link, Command("link"))
    commands.message.register(admin_panel, Command("admin"))
    commands.message.register(pause_all, Command("pause_all"))
    commands.message.register(resume_all, Command("resume_all"))
    commands.message.register(forget, Command("forget"))
    callbacks.callback_query.register(on_button)
    texts.message.register(on_menu_button, F.text.in_(set(MENU_BUTTONS)))
    texts.message.register(on_text)
    dispatcher = Dispatcher()
    dispatcher.include_routers(business_router, commands, callbacks, texts)
    return dispatcher
