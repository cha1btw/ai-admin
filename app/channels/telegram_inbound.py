"""Telegram updates turned into ingest calls: Telegram Business (masters' own accounts) and the
public demo bot. Handlers in app/owner/bot.py and app/channels/telegram_demo.py call these."""

from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Chat, User
from aiogram.types import Message as TgMessage
from sqlalchemy import select

from app.brain.i18n import LANGS
from app.brain.ingest import (
    Inbound,
    get_or_create_conversation,
    record_client_message,
    record_deleted,
    record_edit,
    record_outgoing,
)
from app.brain.language import choose_language
from app.brain.orchestrator import Outgoing
from app.brain.templates import render
from app.channels.accounts import ensure_account
from app.context import AppContext
from app.core.logging import get_logger
from app.db.models import ChannelAccount, Message, Tenant, TenantMember
from app.owner.actions import ensure_owner, set_state
from app.owner.notifications import deliver
from app.tenants.loader import studio_config

log = get_logger(__name__)


def media_of(message: TgMessage) -> tuple[str | None, dict | None]:
    if message.photo:
        return "photo", {"file_id": message.photo[-1].file_id}
    if message.video or message.video_note:
        media = message.video or message.video_note
        return "video", {"file_id": media.file_id}
    if message.voice or message.audio:
        media = message.voice or message.audio
        return "voice", {"file_id": media.file_id}
    if message.sticker:
        return "sticker", None
    if message.document:
        return "file", {"file_id": message.document.file_id}
    return None, None


def _name(chat: Chat | None, user: User | None) -> str | None:
    source = user or chat
    if source is None:
        return None
    parts = [getattr(source, "first_name", None), getattr(source, "last_name", None)]
    name = " ".join(p for p in parts if p)
    username = getattr(source, "username", None)
    return name or (f"@{username}" if username else None)


def to_inbound(message: TgMessage) -> Inbound:
    media_type, media = media_of(message)
    language = None
    if message.from_user and message.from_user.language_code:
        code = message.from_user.language_code.split("-")[0]
        language = code if code in LANGS else None
    return Inbound(
        external_user_id=str(message.chat.id),
        external_message_id=str(message.message_id),
        text=message.text or message.caption,
        media_type=media_type,
        media=media,
        is_story_reply=message.reply_to_story is not None,
        display_name=_name(message.chat, message.from_user),
        language=language,
    )


# --- Telegram Business ------------------------------------------------------------------------


async def _tenant_of_owner(session, owner_id: int) -> Tenant | None:
    tenant_id = await session.scalar(
        select(TenantMember.tenant_id)
        .where(TenantMember.telegram_user_id == owner_id)
        .order_by(TenantMember.id)
        .limit(1)
    )
    return await session.get(Tenant, tenant_id) if tenant_id else None


def _can_reply(connection: BusinessConnection) -> bool:
    if connection.rights is not None:
        return bool(connection.rights.can_reply)
    return bool(getattr(connection, "can_reply", False))


async def on_business_connection(app: AppContext, connection: BusinessConnection) -> None:
    """The master connected, edited or disconnected the bot in Telegram Business settings."""
    owner_id = connection.user.id
    chat_id = connection.user_chat_id
    async with app.session_factory() as session, session.begin():
        tenant = await _tenant_of_owner(session, owner_id)
        if tenant is None:
            owner = await ensure_owner(session, owner_id, _name(None, connection.user))
            set_state(
                owner,
                pending_business_connection={"id": connection.id, "user_chat_id": chat_id},
            )
            text = (
                "Бота підключено до вашого Telegram, але ваш акаунт ще не прив'язаний до студії. "
                "Відкрийте посилання для підключення, яке ми вам надіслали."
            )
        else:
            account = await ensure_account(
                session,
                tenant.id,
                "telegram_business",
                connection.id,
                meta={"user_id": owner_id, "user_chat_id": chat_id},
            )
            account.meta = {
                **(account.meta or {}),
                "user_id": owner_id,
                "user_chat_id": chat_id,
                "can_reply": _can_reply(connection),
            }
            account.status = "active" if connection.is_enabled else "disabled"
            if not connection.is_enabled:
                text = "Бота відключено від вашого Telegram. Клієнткам він більше не відповідає."
            elif not _can_reply(connection):
                text = (
                    "Бот підключений, але не має права відповідати. Увімкніть «Відповідати на "
                    "повідомлення» в Налаштуваннях → Telegram для бізнесу → Чат-боти."
                )
            else:
                text = (
                    f"✅ Бот підключено до вашого Telegram для студії «{tenant.name}». "
                    "Він відповідає у вибраних чатах за режимом студії (/mode)."
                )
    await app.notifier.send(chat_id, text)


async def _business_account(app: AppContext, bot: Bot, session, connection_id: str):
    account = await session.scalar(
        select(ChannelAccount).where(
            ChannelAccount.kind == "telegram_business", ChannelAccount.external_id == connection_id
        )
    )
    if account is not None:
        return account
    try:  # the bot was connected before our server saw the event
        connection = await bot.get_business_connection(business_connection_id=connection_id)
    except TelegramAPIError:
        log.warning("business.connection_unknown")
        return None
    tenant = await _tenant_of_owner(session, connection.user.id)
    if tenant is None:
        return None
    return await ensure_account(
        session,
        tenant.id,
        "telegram_business",
        connection_id,
        meta={
            "user_id": connection.user.id,
            "user_chat_id": connection.user_chat_id,
            "can_reply": _can_reply(connection),
        },
    )


async def on_business_message(app: AppContext, bot: Bot, message: TgMessage) -> None:
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        account = await _business_account(app, bot, session, message.business_connection_id)
        if account is None or account.status != "active":
            return
        owner_id = (account.meta or {}).get("user_id")
        chat_id = str(message.chat.id)
        if message.sender_business_bot is not None:
            # Our own reply seen again: only marks it delivered.
            await record_outgoing(
                session,
                account,
                chat_id,
                str(message.message_id),
                message.text,
                now,
                from_bot=True,
            )
            return
        if message.from_user is not None and message.from_user.id == owner_id:
            # The master writes by hand: the bot steps back (R8).
            await record_outgoing(
                session,
                account,
                chat_id,
                str(message.message_id),
                message.text or message.caption,
                now,
            )
            return
        await record_client_message(
            session,
            account,
            to_inbound(message),
            now,
            debounce=timedelta(seconds=app.settings.debounce_seconds),
        )


async def on_business_edit(app: AppContext, bot: Bot, message: TgMessage) -> None:
    async with app.session_factory() as session, session.begin():
        account = await _business_account(app, bot, session, message.business_connection_id)
        if account is None:
            return
        await record_edit(
            session,
            account,
            str(message.chat.id),
            str(message.message_id),
            message.text or message.caption,
            app.clock.now(),
        )


async def on_business_deleted(app: AppContext, bot: Bot, event: BusinessMessagesDeleted) -> None:
    async with app.session_factory() as session, session.begin():
        account = await _business_account(app, bot, session, event.business_connection_id)
        if account is None:
            return
        await record_deleted(
            session, account, str(event.chat.id), [str(i) for i in event.message_ids]
        )


# --- Demo bot -----------------------------------------------------------------------------


async def _demo_account(session, app: AppContext) -> tuple[ChannelAccount, Tenant] | None:
    tenant = await session.scalar(
        select(Tenant).where(Tenant.slug == app.settings.demo_tenant_slug)
    )
    if tenant is None:
        log.error("demo.tenant_missing", slug=app.settings.demo_tenant_slug)
        return None
    return await ensure_account(session, tenant.id, "telegram_demo", "demo"), tenant


async def on_demo_start(app: AppContext, message: TgMessage) -> None:
    """/start in the public demo bot: a code-made greeting that already discloses the AI (R1)."""
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        found = await _demo_account(session, app)
        if found is None:
            return
        account, tenant = found
        inbound = to_inbound(message)
        client, conversation = await get_or_create_conversation(
            session, account, inbound.external_user_id, now, display_name=inbound.display_name
        )
        cfg = studio_config(tenant)
        if client.language is None and inbound.language:
            client.language = inbound.language
        lang = choose_language(None, client.language, cfg.allowed_languages, cfg.default_language)
        reply = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            role="bot",
            text=render("demo_welcome", lang, studio=cfg.name),
            delivery_status="pending",
            created_at=now,
            config_version=tenant.config_version,
        )
        session.add(reply)
        await session.flush()
        conversation.disclosed_at = now
        conversation.last_bot_message_at = now
        conversation.last_client_message_at = now
        outgoing = [Outgoing(reply.id)]
    await deliver(app, outgoing)


async def on_demo_message(app: AppContext, message: TgMessage) -> None:
    async with app.session_factory() as session, session.begin():
        found = await _demo_account(session, app)
        if found is None:
            return
        account, _ = found
        await record_client_message(
            session,
            account,
            to_inbound(message),
            app.clock.now(),
            debounce=timedelta(seconds=app.settings.debounce_seconds),
        )
