"""What masters and founders do in Owner Bot.

Service functions without aiogram: app/owner/bot.py only maps messages and buttons to these
calls, and tests call them directly. Every action checks that the Telegram account may manage
the studio. Texts are Ukrainian.
"""

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, literal_column, select

from app.brain import booking as bookings
from app.brain.booking import BookingContext, BookingError
from app.brain.events import (
    BookingCancelled,
    BookingCreated,
    BookingRescheduled,
    Event,
    RequestCreated,
)
from app.brain.guardrails import disclosure_needed
from app.brain.handoff import (
    REASON_LABELS,
    eta_text,
    in_owner_hours,
    return_to_bot,
    start_handoff,
    take_over,
)
from app.brain.i18n import WEEKDAYS, day_label, hhmm, money, price_text
from app.brain.ingest import Inbound, record_client_message, supersede_drafts
from app.brain.language import choose_language
from app.brain.orchestrator import Outgoing
from app.brain.replies import (
    booking_text,
    cancelled_text,
    prepayment_confirmed_text,
    request_confirmed_text,
    request_text,
    rescheduled_text,
)
from app.brain.templates import render
from app.channels.accounts import ensure_account
from app.channels.base import window_open
from app.context import AppContext
from app.db.models import (
    ACTIVE_BOOKING_STATUSES,
    AuditLog,
    Block,
    Booking,
    ChannelAccount,
    Client,
    Conversation,
    Draft,
    Feedback,
    InboundEvent,
    LinkToken,
    Message,
    OwnerAccount,
    Service,
    Tenant,
    TenantMember,
)
from app.owner.notifications import deliver, dispatch_events
from app.owner.notifier import Button, Keyboard
from app.owner.parsing import parse_day_range, parse_local_datetime
from app.scheduling.registry import provider_for
from app.tenants.loader import studio_config
from app.tenants.schema import WEEKDAY_KEYS, StudioConfig

LINK_TTL = timedelta(days=3)
MODE_LABELS = {
    "draft": "чернетка (кожну відповідь ви погоджуєте)",
    "offhours": "бот сам відповідає вночі та коли ви зайняті",
    "autopilot": "автопілот (бот відповідає сам)",
}
DRAFT_STATUS = {
    "sent": "уже надіслано",
    "edited": "уже надіслано ваш варіант",
    "rejected": "відхилено",
    "superseded": "клієнтка дописала, є новіша чернетка",
    "expired": "вікно відповіді закрилося",
    "failed": "дію не виконано",
}
BOOKING_ERRORS = {
    "slot_taken": "Цей час уже зайняли. Напишіть клієнтці самі або дочекайтеся нової чернетки.",
    "slot_expired": "Запропонований час застарів. Напишіть клієнтці самі.",
    "late_cancellation": "Запис менш ніж за добу: скасуйте його кнопкою «Скасувати запис».",
    "too_many_active": "У клієнтки вже максимум активних записів.",
    "calendar_unavailable": "Календар зараз недоступний, запис не створено. Спробуйте пізніше.",
}
HELP = (
    "Команди:\n"
    "/mode — режим роботи бота\n"
    "/pause — зупинити бота, /resume — увімкнути\n"
    "/stats — статистика за 7 днів (/stats 30 — за 30)\n"
    "/data — що знає бот про студію\n"
    "/sandbox — спробувати бота як клієнтка, /stop — вийти\n"
    "/today — записи на сьогодні\n"
    "/close 25.10 або /close 25.10-27.10 відпустка — закрити дні, /open 25.10 — відкрити\n"
    "/switch — інша студія, якщо їх кілька\n"
    "/cancel — скасувати поточну дію"
)
ADMIN_HELP = (
    "Для засновників:\n/admin — вхід в адмінку\n/tenants — студії\n"
    "/link <slug> [admin] — посилання для майстра\n"
    "/health — стан системи\n/pause_all, /resume_all — усі студії\n/forget <client_id> — видалити "
    "дані клієнтки"
)


@dataclass
class Result:
    text: str | None
    buttons: Keyboard | None = None
    edits: list[tuple[int, int, str]] = field(default_factory=list)  # (chat, message, text)
    outgoing: list[Outgoing] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)


async def finish(app: AppContext, result: Result) -> Result:
    """Deliver client messages and master notifications produced by an action."""
    result.events += await deliver(app, result.outgoing)
    await dispatch_events(app, result.events)
    return result


# --- accounts and access ----------------------------------------------------------------------


def is_admin(app: AppContext, telegram_id: int) -> bool:
    return telegram_id in app.settings.admin_telegram_ids


async def ensure_owner(session: Any, telegram_id: int, display_name: str | None) -> OwnerAccount:
    account = await session.get(OwnerAccount, telegram_id)
    if account is None:
        account = OwnerAccount(telegram_user_id=telegram_id, display_name=display_name, state={})
        session.add(account)
        await session.flush()
    elif display_name:
        account.display_name = display_name
    return account


async def can_manage(session: Any, app: AppContext, telegram_id: int, tenant_id: int) -> bool:
    if is_admin(app, telegram_id):
        return True
    count = await session.scalar(
        select(func.count())
        .select_from(TenantMember)
        .where(TenantMember.tenant_id == tenant_id, TenantMember.telegram_user_id == telegram_id)
    )
    return bool(count)


async def current_tenant(session: Any, app: AppContext, telegram_id: int) -> Tenant | None:
    account = await session.get(OwnerAccount, telegram_id)
    if (
        account
        and account.active_tenant_id
        and await can_manage(session, app, telegram_id, account.active_tenant_id)
    ):
        return await session.get(Tenant, account.active_tenant_id)
    tenant_id = await session.scalar(
        select(TenantMember.tenant_id)
        .where(TenantMember.telegram_user_id == telegram_id)
        .order_by(TenantMember.id)
        .limit(1)
    )
    if tenant_id is None:
        return None
    if account is not None:
        account.active_tenant_id = tenant_id
    return await session.get(Tenant, tenant_id)


def set_state(account: OwnerAccount, **values: Any) -> None:
    state = dict(account.state or {})
    for key, value in values.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    account.state = state  # reassign so SQLAlchemy sees the JSON change


def _audit(
    app: AppContext,
    session: Any,
    tenant_id: int | None,
    telegram_id: int,
    action: str,
    **details: Any,
) -> None:
    """Audit entries use the app clock, like every other timestamp the bot compares."""
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=f"owner:{telegram_id}",
            action=action,
            details=details,
            created_at=app.clock.now(),
        )
    )


def _edits(refs: list[dict[str, int]] | None, text: str) -> list[tuple[int, int, str]]:
    return [(r["chat_id"], r["message_id"], text) for r in refs or []]


def _lang(cfg: StudioConfig, client: Client) -> str:
    return choose_language(None, client.language, cfg.allowed_languages, cfg.default_language)


def _bot_message(tenant: Tenant, conversation: Conversation, text: str, now: datetime) -> Message:
    return Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        role="bot",
        text=text,
        delivery_status="pending",
        created_at=now,
        config_version=tenant.config_version,
    )


def _contact(client: Client) -> str:
    return client.phone or client.display_name or client.external_user_id


# --- linking -----------------------------------------------------------------------------------


async def create_link(app: AppContext, slug: str, role: str = "owner") -> str | None:
    async with app.session_factory() as session, session.begin():
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        if tenant is None:
            return None
        token = secrets.token_urlsafe(12)
        session.add(
            LinkToken(
                token=token, tenant_id=tenant.id, role=role, expires_at=app.clock.now() + LINK_TTL
            )
        )
        return token


async def link_account(
    app: AppContext, token: str, telegram_id: int, display_name: str | None
) -> Result:
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        link = await session.get(LinkToken, token, with_for_update=True)
        if link is None or link.used_at is not None or link.expires_at <= now:
            return Result("Посилання недійсне або вже використане. Попросіть у нас нове.")
        tenant = await session.get(Tenant, link.tenant_id)
        account = await ensure_owner(session, telegram_id, display_name)
        exists = await session.scalar(
            select(TenantMember).where(
                TenantMember.tenant_id == tenant.id, TenantMember.telegram_user_id == telegram_id
            )
        )
        if exists is None:
            session.add(
                TenantMember(tenant_id=tenant.id, telegram_user_id=telegram_id, role=link.role)
            )
        account.active_tenant_id = tenant.id
        link.used_at, link.used_by = now, telegram_id
        pending = (account.state or {}).get("pending_business_connection")
        if pending:
            await ensure_account(
                session,
                tenant.id,
                "telegram_business",
                pending["id"],
                meta={"user_id": telegram_id, "user_chat_id": pending.get("user_chat_id")},
            )
            set_state(account, pending_business_connection=None)
        _audit(app, session, tenant.id, telegram_id, "member.linked", role=link.role)
        mode = MODE_LABELS.get(tenant.mode, tenant.mode)
        return Result(
            f"Вітаю! Ви підключені до студії «{tenant.name}».\n\n"
            "Що далі:\n/data — перевірте, що знає бот\n/sandbox — спробуйте бота як клієнтка\n"
            f"/mode — режим роботи (зараз: {mode})\n/help — усі команди"
        )


# --- drafts --------------------------------------------------------------------------------


async def approve_draft(app: AppContext, draft_id: int, telegram_id: int) -> Result:
    now = app.clock.now()
    events: list[Event] = []
    async with app.session_factory() as session, session.begin():
        draft = await session.get(Draft, draft_id, with_for_update=True)
        if draft is None or not await can_manage(session, app, telegram_id, draft.tenant_id):
            return Result("Чернетку не знайдено.")
        if draft.status != "pending":
            return Result(
                f"Ця чернетка вже неактуальна: {DRAFT_STATUS.get(draft.status, draft.status)}."
            )
        conversation = await session.get(Conversation, draft.conversation_id)
        tenant = await session.get(Tenant, draft.tenant_id)
        cfg = studio_config(tenant)
        client = await session.get(Client, conversation.client_id)
        account = await session.get(ChannelAccount, conversation.channel_account_id)
        refs = draft.owner_messages
        if conversation.state == "HUMAN_ACTIVE":
            draft.status = "superseded"
            return Result(None, edits=_edits(refs, "Чернетку закрито: ви вже відповіли самі."))
        channel = app.channels.get(account.kind)
        if channel is None or not window_open(channel, conversation, now):
            draft.status = "expired"
            text = (
                "Вікно відповіді закрилося (24 год після останнього повідомлення клієнтки). "
                f"Напишіть їй самі: {_contact(client)}"
            )
            return Result(text, edits=_edits(refs, f"⌛ Чернетка застаріла.\n{draft.text}"))

        lang = _lang(cfg, client)
        action = draft.proposed_action or {}
        tool, args = action.get("tool"), action.get("args", {})
        ctx = BookingContext(
            session, tenant, cfg, provider_for(app.calendars, cfg), now, conversation, client
        )
        text = draft.text
        try:
            if tool == "create_booking":
                view = await bookings.create_booking(
                    ctx, args["slot_id"], args.get("client_name"), args.get("client_phone")
                )
                text = booking_text(cfg, view, lang)
                events.append(BookingCreated(tenant.id, view.id))
            elif tool == "cancel_booking":
                view = await bookings.cancel_booking(ctx, int(args["booking_id"]))
                text = cancelled_text(cfg, view, lang)
                events.append(BookingCancelled(tenant.id, view.id))
            elif tool == "reschedule_booking":
                old, new = await bookings.reschedule_booking(
                    ctx, int(args["booking_id"]), args["slot_id"]
                )
                text = rescheduled_text(cfg, new, lang)
                events.append(BookingRescheduled(tenant.id, old.id, new.id))
            elif tool == "request_booking":
                view = await bookings.request_booking(
                    ctx,
                    args["service_id"],
                    args["preferred_time_text"],
                    args.get("client_name"),
                    args.get("client_phone"),
                )
                text = request_text(view, lang, eta_text(cfg, now, lang))
                events.append(RequestCreated(tenant.id, view.id))
            elif tool == "handoff_to_human":
                start_handoff(conversation, args.get("reason", "other"), now)
                _audit(
                    app,
                    session,
                    tenant.id,
                    telegram_id,
                    "handoff",
                    reason=args.get("reason", "other"),
                    conversation_id=conversation.id,
                )
        except BookingError as exc:
            draft.status, draft.result = "failed", {"error": exc.code}
            message = BOOKING_ERRORS.get(exc.code, f"Дію не виконано ({exc.code}).")
            return Result(message, edits=_edits(refs, f"⚠️ Не надіслано: {message}"))

        disclosure = disclosure_needed(conversation, now, cfg.disclosure_silence_days)
        final = f"{render(disclosure, lang, studio=cfg.name)} {text}" if disclosure else text
        message = _bot_message(tenant, conversation, final, now)
        session.add(message)
        await session.flush()
        if disclosure:
            conversation.disclosed_at = now
        if cfg.profile.privacy_url and str(cfg.profile.privacy_url) in final:
            client.privacy_notified_at = now
        conversation.last_bot_message_at = now
        draft.status, draft.decided_at, draft.decided_by = "sent", now, telegram_id
        draft.result = {"message_id": message.id}
        _audit(app, session, tenant.id, telegram_id, "draft.sent", draft_id=draft.id, tool=tool)
        outgoing = [Outgoing(message.id)]
        edits = _edits(refs, f"✅ Надіслано:\n{final}")
    return await finish(app, Result(None, edits=edits, outgoing=outgoing, events=events))


async def reject_draft(app: AppContext, draft_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        draft = await session.get(Draft, draft_id, with_for_update=True)
        if draft is None or not await can_manage(session, app, telegram_id, draft.tenant_id):
            return Result("Чернетку не знайдено.")
        if draft.status != "pending":
            return Result(
                f"Ця чернетка вже неактуальна: {DRAFT_STATUS.get(draft.status, draft.status)}."
            )
        draft.status, draft.decided_at, draft.decided_by = "rejected", app.clock.now(), telegram_id
        _audit(app, session, draft.tenant_id, telegram_id, "draft.rejected", draft_id=draft.id)
        return Result(
            None, edits=_edits(draft.owner_messages, "❌ Відхилено. Клієнтці нічого не надіслано.")
        )


async def start_edit_draft(app: AppContext, draft_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        draft = await session.get(Draft, draft_id)
        if draft is None or not await can_manage(session, app, telegram_id, draft.tenant_id):
            return Result("Чернетку не знайдено.")
        if draft.status != "pending":
            return Result(
                f"Ця чернетка вже неактуальна: {DRAFT_STATUS.get(draft.status, draft.status)}."
            )
        account = await ensure_owner(session, telegram_id, None)
        set_state(account, awaiting={"kind": "edit", "draft_id": draft.id})
        return Result("Напишіть свій варіант відповіді одним повідомленням. /cancel — скасувати.")


async def finish_edit_draft(app: AppContext, telegram_id: int, draft_id: int, text: str) -> Result:
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        account = await ensure_owner(session, telegram_id, None)
        set_state(account, awaiting=None)
        draft = await session.get(Draft, draft_id, with_for_update=True)
        if draft is None or draft.status != "pending":
            return Result("Ця чернетка вже неактуальна, ваш текст не надіслано.")
        conversation = await session.get(Conversation, draft.conversation_id)
        client = await session.get(Client, conversation.client_id)
        account_row = await session.get(ChannelAccount, conversation.channel_account_id)
        channel = app.channels.get(account_row.kind)
        if channel is None or not window_open(channel, conversation, now):
            draft.status = "expired"
            return Result(f"Вікно відповіді закрилося. Напишіть клієнтці самі: {_contact(client)}")
        message = Message(
            tenant_id=draft.tenant_id,
            conversation_id=conversation.id,
            role="human",
            text=text,
            delivery_status="pending",
            created_at=now,
        )
        session.add(message)
        await session.flush()
        draft.status, draft.decided_at, draft.decided_by = "edited", now, telegram_id
        _audit(app, session, draft.tenant_id, telegram_id, "draft.edited", draft_id=draft.id)
        edits = _edits(draft.owner_messages, f"✏️ Надіслано ваш варіант:\n{text}")
        outgoing = [Outgoing(message.id)]
    return await finish(app, Result("Надіслано.", edits=edits, outgoing=outgoing))


# --- conversations -------------------------------------------------------------------------


async def _conversation_for(session, app, telegram_id, conversation_id) -> Conversation | None:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or not await can_manage(
        session, app, telegram_id, conversation.tenant_id
    ):
        return None
    return conversation


async def take_conversation(app: AppContext, conversation_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        conversation = await _conversation_for(session, app, telegram_id, conversation_id)
        if conversation is None:
            return Result("Діалог не знайдено.")
        take_over(conversation, app.clock.now())
        conversation.reply_due_at = None
        await supersede_drafts(session, conversation.id)
        tenant = await session.get(Tenant, conversation.tenant_id)
        hours = studio_config(tenant).handoff.resume_hours
        _audit(
            app,
            session,
            tenant.id,
            telegram_id,
            "conversation.taken",
            conversation_id=conversation.id,
        )
        return Result(
            "Діалог ваш: бот мовчить, доки ви не натиснете «Повернути бота» "
            f"або доки не мине {hours} год без ваших повідомлень."
        )


async def return_bot(app: AppContext, conversation_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        conversation = await _conversation_for(session, app, telegram_id, conversation_id)
        if conversation is None:
            return Result("Діалог не знайдено.")
        return_to_bot(conversation)
        _audit(
            app,
            session,
            conversation.tenant_id,
            telegram_id,
            "conversation.returned",
            conversation_id=conversation.id,
        )
        return Result(
            "Бот знову відповідає в цьому діалозі. На вже отримані повідомлення він не "
            "відповідатиме, лише на нові."
        )


async def start_reply(app: AppContext, conversation_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        conversation = await _conversation_for(session, app, telegram_id, conversation_id)
        if conversation is None:
            return Result("Діалог не знайдено.")
        account = await ensure_owner(session, telegram_id, None)
        set_state(account, awaiting={"kind": "reply", "conversation_id": conversation.id})
        return Result(
            "Напишіть відповідь клієнтці одним повідомленням, вона піде від імені студії. "
            "Діалог стане вашим, бот замовкне. /cancel — скасувати."
        )


async def finish_reply(
    app: AppContext, telegram_id: int, conversation_id: int, text: str
) -> Result:
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        account = await ensure_owner(session, telegram_id, None)
        set_state(account, awaiting=None)
        conversation = await _conversation_for(session, app, telegram_id, conversation_id)
        if conversation is None:
            return Result("Діалог не знайдено.")
        client = await session.get(Client, conversation.client_id)
        account_row = await session.get(ChannelAccount, conversation.channel_account_id)
        channel = app.channels.get(account_row.kind)
        if channel is None or not window_open(channel, conversation, now):
            return Result(
                "Вікно відповіді закрилося (24 год після останнього повідомлення клієнтки). "
                f"Напишіть їй самі: {_contact(client)}"
            )
        message = Message(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            role="human",
            text=text,
            delivery_status="pending",
            created_at=now,
        )
        session.add(message)
        await session.flush()
        take_over(conversation, now)
        conversation.reply_due_at = None
        await supersede_drafts(session, conversation.id)
        _audit(
            app,
            session,
            conversation.tenant_id,
            telegram_id,
            "conversation.replied",
            conversation_id=conversation.id,
        )
        outgoing = [Outgoing(message.id)]
    return await finish(app, Result("Надіслано. Діалог ваш, бот мовчить.", outgoing=outgoing))


# --- bookings and requests ------------------------------------------------------------------


async def _booking_ctx(
    session, app, telegram_id, booking_id
) -> tuple[Booking, BookingContext] | None:
    booking = await session.get(Booking, booking_id)
    if booking is None or not await can_manage(session, app, telegram_id, booking.tenant_id):
        return None
    tenant = await session.get(Tenant, booking.tenant_id)
    cfg = studio_config(tenant)
    conversation = (
        await session.get(Conversation, booking.conversation_id)
        if booking.conversation_id
        else None
    )
    client = await session.get(Client, booking.client_id) if booking.client_id else None
    ctx = BookingContext(
        session,
        tenant,
        cfg,
        provider_for(app.calendars, cfg),
        app.clock.now(),
        conversation,
        client,
    )
    return booking, ctx


def _when(cfg: StudioConfig, booking: Booking) -> str:
    if booking.start_at is None:
        return f"бажаний час: {booking.requested_time_text}"
    local = booking.start_at.astimezone(ZoneInfo(cfg.timezone))
    return f"{day_label(local.date(), 'uk')} о {hhmm(local)}"


async def ask_cancel_booking(app: AppContext, booking_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session:
        found = await _booking_ctx(session, app, telegram_id, booking_id)
        if found is None:
            return Result("Запис не знайдено.")
        booking, ctx = found
        if booking.status not in ACTIVE_BOOKING_STATUSES:
            return Result("Цей запис уже не активний.")
        return Result(
            f"Скасувати запис {booking.client_name or 'клієнтки'} · {_when(ctx.cfg, booking)}?",
            buttons=[[Button("Так, скасувати", f"b:cx:{booking.id}"), Button("Ні", "noop")]],
        )


async def cancel_booking_by_studio(app: AppContext, booking_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        found = await _booking_ctx(session, app, telegram_id, booking_id)
        if found is None:
            return Result("Запис не знайдено.")
        booking, ctx = found
        try:
            await bookings.cancel_booking(
                ctx, booking.id, by="studio", reason="cancelled_by_master"
            )
        except BookingError as exc:
            return Result(
                {"not_active": "Цей запис уже не активний.", "in_past": "Цей запис уже минув."}.get(
                    exc.code, exc.code
                )
            )
        _audit(
            app, session, booking.tenant_id, telegram_id, "booking.cancelled", booking_id=booking.id
        )
        contact = booking.client_phone or (ctx.client.display_name if ctx.client else None) or "—"
        return Result(
            "Запис скасовано. Бот не пише клієнтці сам: повідомте її, будь ласка. "
            f"Контакт: {contact}"
        )


async def _client_message_if_open(session, app, ctx: BookingContext, text: str) -> list[Outgoing]:
    """A code-made message to the client, only inside the channel window (R11)."""
    if ctx.conversation is None:
        return []
    account = await session.get(ChannelAccount, ctx.conversation.channel_account_id)
    channel = app.channels.get(account.kind)
    if channel is None or not window_open(channel, ctx.conversation, ctx.now):
        return []
    message = _bot_message(ctx.tenant, ctx.conversation, text, ctx.now)
    session.add(message)
    await session.flush()
    ctx.conversation.last_bot_message_at = ctx.now
    return [Outgoing(message.id)]


async def confirm_payment(app: AppContext, booking_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        found = await _booking_ctx(session, app, telegram_id, booking_id)
        if found is None:
            return Result("Запис не знайдено.")
        booking, ctx = found
        try:
            view = await bookings.confirm_prepayment(ctx, booking.id)
        except BookingError:
            return Result("Цей запис не чекає на передоплату.")
        outgoing = []
        if ctx.client is not None:
            text = prepayment_confirmed_text(ctx.cfg, view, _lang(ctx.cfg, ctx.client))
            outgoing = await _client_message_if_open(session, app, ctx, text)
        _audit(app, session, booking.tenant_id, telegram_id, "booking.paid", booking_id=booking.id)
    note = "клієнтці надіслано підтвердження" if outgoing else "повідомте клієнтку самі"
    return await finish(
        app, Result(f"Оплату відмічено, запис підтверджено; {note}.", outgoing=outgoing)
    )


async def start_confirm_request(app: AppContext, booking_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        found = await _booking_ctx(session, app, telegram_id, booking_id)
        if found is None or found[0].status != "requested":
            return Result("Заявку не знайдено або її вже оброблено.")
        account = await ensure_owner(session, telegram_id, None)
        set_state(account, awaiting={"kind": "confirm_time", "booking_id": booking_id})
        return Result("Напишіть дату й час запису, наприклад: 23.10 15:30 або завтра 15:30.")


async def finish_confirm_request(
    app: AppContext, telegram_id: int, booking_id: int, text: str
) -> Result:
    async with app.session_factory() as session, session.begin():
        found = await _booking_ctx(session, app, telegram_id, booking_id)
        if found is None:
            return Result("Заявку не знайдено.")
        booking, ctx = found
        start = parse_local_datetime(text, ctx.now.astimezone(ZoneInfo(ctx.cfg.timezone)))
        if start is None:
            return Result("Не зрозумів дату й час. Приклад: 23.10 15:30 або завтра 15:30.")
        account = await ensure_owner(session, telegram_id, None)
        set_state(account, awaiting=None)
        try:
            view = await bookings.confirm_request(ctx, booking.id, start)
        except BookingError:
            return Result("Заявку вже оброблено або час у минулому.")
        outgoing = []
        if ctx.client is not None:
            text_out = request_confirmed_text(ctx.cfg, view, _lang(ctx.cfg, ctx.client))
            outgoing = await _client_message_if_open(session, app, ctx, text_out)
        _audit(
            app, session, booking.tenant_id, telegram_id, "request.confirmed", booking_id=booking.id
        )
        when = _when(ctx.cfg, booking)
    note = (
        "клієнтці надіслано підтвердження"
        if outgoing
        else (f"вікно відповіді закрилося, повідомте клієнтку самі: {booking.client_phone}")
    )
    return await finish(app, Result(f"Запис підтверджено: {when}; {note}.", outgoing=outgoing))


async def reject_request(app: AppContext, booking_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        found = await _booking_ctx(session, app, telegram_id, booking_id)
        if found is None or found[0].status != "requested":
            return Result("Заявку не знайдено або її вже оброблено.")
        booking, ctx = found
        await bookings.reject_request(ctx, booking.id)
        outgoing = []
        if ctx.client is not None:
            outgoing = await _client_message_if_open(
                session, app, ctx, render("request_rejected", _lang(ctx.cfg, ctx.client))
            )
        _audit(
            app, session, booking.tenant_id, telegram_id, "request.rejected", booking_id=booking.id
        )
    note = "клієнтці запропоновано інший час" if outgoing else "повідомте клієнтку самі"
    return await finish(app, Result(f"Заявку відхилено; {note}.", outgoing=outgoing))


async def other_time(app: AppContext, booking_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session:
        found = await _booking_ctx(session, app, telegram_id, booking_id)
        if found is None or found[0].conversation_id is None:
            return Result("Заявку не знайдено.")
        conversation_id = found[0].conversation_id
    await take_conversation(app, conversation_id, telegram_id)
    result = await start_reply(app, conversation_id, telegram_id)
    result.text = "Напишіть клієнтці, який час можете запропонувати. " + (result.text or "")
    return result


# --- feedback, modes, pause ------------------------------------------------------------------


async def feedback(app: AppContext, message_id: int, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        message = await session.get(Message, message_id)
        if message is None or not await can_manage(session, app, telegram_id, message.tenant_id):
            return Result("Повідомлення не знайдено.")
        row = Feedback(
            tenant_id=message.tenant_id,
            message_id=message.id,
            telegram_user_id=telegram_id,
            verdict="down",
        )
        session.add(row)
        await session.flush()
        account = await ensure_owner(session, telegram_id, None)
        set_state(account, awaiting={"kind": "feedback", "feedback_id": row.id})
        return Result("Дякуємо! Що саме не так? Напишіть одним повідомленням або /skip.")


async def finish_feedback(app: AppContext, telegram_id: int, feedback_id: int, text: str) -> Result:
    async with app.session_factory() as session, session.begin():
        account = await ensure_owner(session, telegram_id, None)
        set_state(account, awaiting=None)
        row = await session.get(Feedback, feedback_id)
        if row is None:
            return Result(None)
        if text.strip() != "/skip":
            row.comment = text.strip()[:2000]
        tenant = await session.get(Tenant, row.tenant_id)
    await app.alerts.send(
        f"feedback:{feedback_id}", f"👎 від студії {tenant.slug} (відгук #{feedback_id})."
    )
    return Result("Записали, розберемо цей випадок.")


def mode_keyboard() -> Keyboard:
    return [
        [Button("Чернетка", "m:draft")],
        [Button("Ніч і зайняті години", "m:offhours")],
        [Button("Автопілот", "m:autopilot")],
    ]


async def show_mode(app: AppContext, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        tenant = await current_tenant(session, app, telegram_id)
        if tenant is None:
            return Result(NOT_LINKED)
        return Result(
            f"Зараз: {MODE_LABELS[tenant.mode]}.\nОберіть режим:", buttons=mode_keyboard()
        )


async def set_mode(app: AppContext, telegram_id: int, mode: str) -> Result:
    if mode not in MODE_LABELS:
        return Result("Невідомий режим.")
    async with app.session_factory() as session, session.begin():
        tenant = await current_tenant(session, app, telegram_id)
        if tenant is None:
            return Result(NOT_LINKED)
        tenant.mode = mode
        _audit(app, session, tenant.id, telegram_id, "tenant.mode", mode=mode)
        return Result(f"Режим змінено: {MODE_LABELS[mode]}.")


async def pause(app: AppContext, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        tenant = await current_tenant(session, app, telegram_id)
        if tenant is None:
            return Result(NOT_LINKED)
        tenant.status = "paused"
        _audit(app, session, tenant.id, telegram_id, "tenant.paused")
        return Result("Бот на паузі: він нічого не надсилає клієнткам. /resume — увімкнути.")


async def resume(app: AppContext, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        tenant = await current_tenant(session, app, telegram_id)
        if tenant is None:
            return Result(NOT_LINKED)
        paused_at = await session.scalar(
            select(AuditLog.created_at)
            .where(AuditLog.tenant_id == tenant.id, AuditLog.action == "tenant.paused")
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
        waiting = 0
        if paused_at is not None:
            waiting = await session.scalar(
                select(func.count(func.distinct(Message.conversation_id))).where(
                    Message.tenant_id == tenant.id,
                    Message.role == "client",
                    Message.created_at >= paused_at,
                )
            )
        tenant.status = "active"
        _audit(app, session, tenant.id, telegram_id, "tenant.resumed")
    tail = (
        f" Поки бот був на паузі, написали {waiting} клієнток: бот їм не відповідатиме, "
        "дайте відповідь самі."
        if waiting
        else ""
    )
    return Result("Бот знову працює." + tail)


NOT_LINKED = "Ваш акаунт ще не підключений до студії. Попросіть у нас посилання для підключення."


# --- information -------------------------------------------------------------------------


def _ranges_text(ranges: list) -> str:
    return ", ".join(f"{r.start:%H:%M}–{r.end:%H:%M}" for r in ranges) or "вихідний"


def data_text(cfg: StudioConfig) -> str:
    lines = [f"Що знає бот про «{cfg.name}»:", f"Адреса: {cfg.profile.address}"]
    if cfg.profile.how_to_find:
        lines.append(f"Як знайти: {cfg.profile.how_to_find}")
    lines.append("Години:")
    for index in range(len(WEEKDAY_KEYS)):
        lines.append(f"  {WEEKDAYS['uk'][index]}: {_ranges_text(cfg.hours.for_weekday(index))}")
    lines.append("Послуги:")
    for service in cfg.services:
        masters = ", ".join(m.name for m in cfg.staff_for_service(service.id))
        price = price_text(
            service.price_type, service.price_min, service.price_max, cfg.currency, "uk"
        )
        lines.append(f"  • {service.name}: {price}, {service.duration_min} хв ({masters})")
    if cfg.prepayment.required:
        lines.append(
            f"Передоплата: так, до {cfg.prepayment.deadline_hours} год; реквізити: "
            f"{cfg.prepayment.requisites_text}"
        )
    else:
        lines.append("Передоплата: не потрібна")
    rule = cfg.cancellation.late_policy_text
    lines.append(
        f"Скасування: безкоштовно за {cfg.cancellation.free_cancel_hours} год."
        + (f" {rule}" if rule else "")
    )
    if cfg.faq:
        lines.append(f"Часті питання ({len(cfg.faq)}): " + "; ".join(f.q for f in cfg.faq))
    lines.append("Мови: " + ", ".join(cfg.allowed_languages))
    lines.append("\nЯкщо щось не так, напишіть нам: виправимо до запуску.")
    return "\n".join(lines)


async def show_data(app: AppContext, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        tenant = await current_tenant(session, app, telegram_id)
        if tenant is None:
            return Result(NOT_LINKED)
        return Result(data_text(studio_config(tenant)))


async def stats_text(app: AppContext, tenant: Tenant, days: int) -> str:
    cfg = studio_config(tenant)
    now = app.clock.now()
    since = now - timedelta(days=days)
    async with app.session_factory() as session:
        real = select(Conversation.id).where(
            Conversation.tenant_id == tenant.id, Conversation.is_sandbox.is_(False)
        )
        conversations = await session.scalar(
            select(func.count(func.distinct(Message.conversation_id))).where(
                Message.role == "client",
                Message.created_at >= since,
                Message.conversation_id.in_(real),
            )
        )
        rows = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(Booking.price_min), 0)).where(
                    Booking.tenant_id == tenant.id,
                    Booking.created_by == "bot",
                    Booking.is_sandbox.is_(False),
                    Booking.created_at >= since,
                    Booking.status.in_(("confirmed", "pending_prepayment")),
                )
            )
        ).one()
        client_times = list(
            await session.scalars(
                select(Message.created_at).where(
                    Message.role == "client",
                    Message.created_at >= since,
                    Message.conversation_id.in_(real),
                )
            )
        )
        reason = literal_column("audit_log.details->>'reason'")
        handoffs = (
            await session.execute(
                select(reason, func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.tenant_id == tenant.id,
                    AuditLog.action == "handoff",
                    AuditLog.created_at >= since,
                )
                .group_by(reason)
            )
        ).all()
        downs = await session.scalar(
            select(func.count())
            .select_from(Feedback)
            .where(Feedback.tenant_id == tenant.id, Feedback.created_at >= since)
        )
        failed = await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.tenant_id == tenant.id,
                Message.delivery_status == "failed",
                Message.created_at >= since,
            )
        )
        tokens = await session.scalar(
            select(func.coalesce(func.sum(Message.tokens_in + Message.tokens_out), 0)).where(
                Message.tenant_id == tenant.id, Message.created_at >= since
            )
        )
        first_reply = await _median_first_reply(session, tenant.id, since)
    off = sum(1 for t in client_times if not in_owner_hours(cfg, t))
    share = f"{round(100 * off / len(client_times))}%" if client_times else "—"
    handoff_text = ", ".join(f"{REASON_LABELS.get(r, r)}: {n}" for r, n in handoffs) or "немає"
    count, amount = rows
    return (
        f"Статистика за {days} дн. ({tenant.name}):\n"
        f"Діалоги: {conversations}\n"
        f"Записи від бота: {count} на суму {money(int(amount), cfg.currency, 'uk')}\n"
        f"Повідомлення поза вашими годинами: {share}\n"
        f"Медіана першої відповіді: {first_reply}\n"
        f"Передано вам: {handoff_text}\n"
        f"👎 на відповіді бота: {downs}\nНе доставлено: {failed}\n"
        f"Токени моделі: {tokens}"
    )


async def _median_first_reply(session, tenant_id: int, since: datetime) -> str:
    """Median time from a client's first message in a quiet conversation to the bot's answer."""
    rows = (
        await session.execute(
            select(Message.conversation_id, Message.role, Message.created_at)
            .where(
                Message.tenant_id == tenant_id,
                Message.created_at >= since,
                Message.role.in_(("client", "bot")),
            )
            .order_by(Message.conversation_id, Message.id)
        )
    ).all()
    waits: list[float] = []
    pending: dict[int, datetime] = {}
    for conversation_id, role, created in rows:
        if role == "client":
            pending.setdefault(conversation_id, created)
        elif conversation_id in pending:
            waits.append((created - pending.pop(conversation_id)).total_seconds())
    if not waits:
        return "—"
    waits.sort()
    median = waits[len(waits) // 2]
    return f"{int(median)} с" if median < 120 else f"{int(median // 60)} хв"


async def show_stats(app: AppContext, telegram_id: int, days: int) -> Result:
    async with app.session_factory() as session, session.begin():
        tenant = await current_tenant(session, app, telegram_id)
    if tenant is None:
        return Result(NOT_LINKED)
    return Result(await stats_text(app, tenant, max(1, min(days, 365))))


async def today_text(app: AppContext, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        tenant = await current_tenant(session, app, telegram_id)
        if tenant is None:
            return Result(NOT_LINKED)
        cfg = studio_config(tenant)
        tz = ZoneInfo(cfg.timezone)
        local = app.clock.now().astimezone(tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        rows = list(
            await session.scalars(
                select(Booking)
                .where(
                    Booking.tenant_id == tenant.id,
                    Booking.is_sandbox.is_(False),
                    Booking.status.in_(("confirmed", "pending_prepayment")),
                    Booking.start_at >= start,
                    Booking.start_at < end,
                )
                .order_by(Booking.start_at)
            )
        )
        lines = [f"Записи через бота на {day_label(local.date(), 'uk')}:"]
        for booking in rows:
            service = await session.get(Service, booking.service_id)
            when = booking.start_at.astimezone(tz)
            lines.append(
                f"{hhmm(when)} · {service.name} · {booking.client_name or '—'} · "
                f"{booking.client_phone or 'без телефону'}"
            )
        if not rows:
            lines.append("немає")
        return Result("\n".join(lines))


async def close_days(
    app: AppContext, telegram_id: int, text: str, *, reopen: bool = False
) -> Result:
    async with app.session_factory() as session, session.begin():
        tenant = await current_tenant(session, app, telegram_id)
        if tenant is None:
            return Result(NOT_LINKED)
        cfg = studio_config(tenant)
        if cfg.calendar_provider != "internal":
            return Result(
                "Ваша студія веде запис у власному календарі: закрийте час там "
                "(подія із позначкою «Зайнято»)."
            )
        tz = ZoneInfo(cfg.timezone)
        today = app.clock.now().astimezone(tz).date()
        parsed = parse_day_range(text, today)
        if parsed is None:
            return Result("Приклад: /close 25.10 або /close 25.10-27.10 відпустка.")
        first, last, reason = parsed
        start = datetime.combine(first, datetime.min.time(), tzinfo=tz)
        end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=tz)
        if reopen:
            await session.execute(
                delete(Block).where(
                    Block.tenant_id == tenant.id, Block.start_at < end, Block.end_at > start
                )
            )
            _audit(
                app,
                session,
                tenant.id,
                telegram_id,
                "days.opened",
                first=str(first),
                last=str(last),
            )
            return Result(f"Відкрито: {day_label(first, 'uk')} – {day_label(last, 'uk')}.")
        session.add(
            Block(
                tenant_id=tenant.id,
                start_at=start,
                end_at=end,
                reason=reason or None,
                created_by=f"owner:{telegram_id}",
            )
        )
        clashes = await session.scalar(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.tenant_id == tenant.id,
                Booking.is_sandbox.is_(False),
                Booking.status.in_(("confirmed", "pending_prepayment")),
                Booking.start_at < end,
                Booking.end_at > start,
            )
        )
        _audit(
            app, session, tenant.id, telegram_id, "days.closed", first=str(first), last=str(last)
        )
    tail = (
        f"\nУвага: на ці дні вже є записів: {clashes}. Бот клієнткам не пише, повідомте їх "
        "самі (/today показує контакти)."
        if clashes
        else ""
    )
    return Result(f"Закрито: {day_label(first, 'uk')} – {day_label(last, 'uk')}.{tail}")


# --- sandbox -----------------------------------------------------------------------------


async def start_sandbox(app: AppContext, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        tenant = await current_tenant(session, app, telegram_id)
        if tenant is None:
            return Result(NOT_LINKED)
        account = await ensure_owner(session, telegram_id, None)
        channel = await ensure_account(session, tenant.id, "sandbox", f"sandbox-{tenant.id}")
        await session.execute(
            delete(Client).where(
                Client.channel_account_id == channel.id, Client.external_user_id == str(telegram_id)
            )
        )
        set_state(account, sandbox_tenant=tenant.id, awaiting=None)
        return Result(
            "🧪 Пісочниця: пишіть боту як клієнтка, на даних вашої студії. Записи пробні, "
            "календар не змінюється, сповіщень не буде.\n/stop — вийти."
        )


async def stop_sandbox(app: AppContext, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        account = await ensure_owner(session, telegram_id, None)
        set_state(account, sandbox_tenant=None)
    return Result("Пісочницю закрито.")


async def sandbox_message(
    app: AppContext, telegram_id: int, tenant_id: int, inbound: Inbound
) -> None:
    async with app.session_factory() as session, session.begin():
        channel = await ensure_account(session, tenant_id, "sandbox", f"sandbox-{tenant_id}")
        await record_client_message(
            session,
            channel,
            inbound,
            app.clock.now(),
            debounce=timedelta(seconds=app.settings.debounce_seconds),
            sandbox=True,
        )


# --- text router ---------------------------------------------------------------------------


async def handle_text(app: AppContext, telegram_id: int, text: str) -> Result | None:
    """Plain text in Owner Bot: finish a pending action, talk in the sandbox, or show help."""
    async with app.session_factory() as session:
        account = await session.get(OwnerAccount, telegram_id)
        state = dict(account.state or {}) if account else {}
    awaiting = state.get("awaiting") or {}
    kind = awaiting.get("kind")
    if kind == "reply":
        return await finish_reply(app, telegram_id, awaiting["conversation_id"], text)
    if kind == "edit":
        return await finish_edit_draft(app, telegram_id, awaiting["draft_id"], text)
    if kind == "confirm_time":
        return await finish_confirm_request(app, telegram_id, awaiting["booking_id"], text)
    if kind == "feedback":
        return await finish_feedback(app, telegram_id, awaiting["feedback_id"], text)
    if state.get("sandbox_tenant"):
        await sandbox_message(
            app, telegram_id, state["sandbox_tenant"], Inbound(str(telegram_id), None, text)
        )
        return None
    return Result("Не зрозумів. " + HELP)


async def cancel_pending(app: AppContext, telegram_id: int) -> Result:
    async with app.session_factory() as session, session.begin():
        account = await ensure_owner(session, telegram_id, None)
        set_state(account, awaiting=None)
    return Result("Скасовано.")


# --- founders --------------------------------------------------------------------------------


async def tenants_text(app: AppContext) -> str:
    async with app.session_factory() as session:
        rows = (
            await session.execute(
                select(Tenant, func.count(TenantMember.id))
                .outerjoin(TenantMember, TenantMember.tenant_id == Tenant.id)
                .group_by(Tenant.id)
                .order_by(Tenant.id)
            )
        ).all()
    lines = [
        f"{t.slug}: {t.mode}, {t.status}, учасників {n}, конфіг v{t.config_version}"
        for t, n in rows
    ]
    return "\n".join(lines) or "Студій немає."


async def health_text(app: AppContext) -> str:
    async with app.session_factory() as session:
        pending = await session.scalar(
            select(func.count()).select_from(InboundEvent).where(InboundEvent.status == "pending")
        )
        failed = await session.scalar(
            select(func.count()).select_from(InboundEvent).where(InboundEvent.status == "failed")
        )
        due = await session.scalar(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.reply_due_at <= app.clock.now())
        )
    return (
        f"БД: ok\nПодії в черзі: {pending}, з помилкою: {failed}\nДіалогів чекають відповіді: {due}"
    )


async def set_all_status(app: AppContext, telegram_id: int, status: str) -> Result:
    async with app.session_factory() as session, session.begin():
        tenants = list(await session.scalars(select(Tenant)))
        for tenant in tenants:
            tenant.status = status
        session.add(
            AuditLog(
                tenant_id=None, actor=f"admin:{telegram_id}", action=f"all.{status}", details={}
            )
        )
    return Result(f"Усі студії: {status} ({len(tenants)}).")
