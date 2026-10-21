"""After-commit side effects: deliver bot replies to clients and tell the master what happened.

Notifications are in Ukrainian and come without sound outside the master's hours. Sandbox
conversations never notify anyone.
"""

from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.brain.booking import booking_view
from app.brain.events import (
    BookingCancelled,
    BookingCreated,
    BookingRescheduled,
    DraftCreated,
    Event,
    HandoffStarted,
    MediaReceived,
    RequestCreated,
    SendFailed,
)
from app.brain.handoff import REASON_LABELS, in_owner_hours
from app.brain.i18n import day_label, hhmm, money, price_text
from app.channels.base import ChannelError, window_open
from app.context import AppContext
from app.core.logging import get_logger
from app.db.models import (
    Booking,
    ChannelAccount,
    Client,
    Conversation,
    Draft,
    Message,
    Tenant,
    TenantMember,
)
from app.owner.notifier import Button, Keyboard
from app.tenants.loader import studio_config
from app.tenants.schema import StudioConfig

log = get_logger(__name__)

CHANNEL_LABELS = {
    "telegram_business": "Telegram",
    "instagram": "Instagram",
    "telegram_demo": "демо-бот",
    "cli": "термінал",
    "sandbox": "пісочниця",
}
MEDIA_NAMES = {"photo": "фото", "video": "відео", "file": "файл"}
ACTION_NOTES = {
    "create_booking": "Після «Відправити» бот створить цей запис.",
    "reschedule_booking": "Після «Відправити» бот перенесе запис.",
    "cancel_booking": "Після «Відправити» бот скасує запис.",
    "request_booking": "Після «Відправити» бот створить заявку.",
    "handoff_to_human": "Після «Відправити» діалог перейде до вас, бот замовкне.",
}


# --- delivery to clients -------------------------------------------------------------------


async def deliver(app: AppContext, outgoing: list) -> list[Event]:
    """Send stored replies. R8: a bot reply is dropped if a human took over meanwhile.
    R11: nothing is sent outside the channel window; the master gets the text instead."""
    events: list[Event] = []
    for item in outgoing:
        async with app.session_factory() as session:
            message = await session.get(Message, item.message_id)
            if message is None or message.text is None:
                continue
            conversation = await session.get(Conversation, message.conversation_id)
            account = await session.get(ChannelAccount, conversation.channel_account_id)
            client = await session.get(Client, conversation.client_id)
            channel = app.channels.get(account.kind)
            now = app.clock.now()
            if message.role == "bot" and conversation.state == "HUMAN_ACTIVE":
                message.delivery_status = "cancelled"
                await session.commit()
                continue
            if channel is None or not window_open(channel, conversation, now):
                message.delivery_status = "failed"
                await session.commit()
                reason = "закрилося вікно відповіді" if channel else "канал не налаштований"
                events.append(
                    SendFailed(conversation.tenant_id, conversation.id, message.text, reason)
                )
                continue
            recipient, text = client.external_user_id, message.text
            await session.commit()
            try:
                ids = await channel.send_text(account, recipient, text)
            except ChannelError as exc:
                message.delivery_status = "failed"
                await session.commit()
                log.warning("send.failed", conversation_id=conversation.id, permanent=exc.permanent)
                if not exc.permanent:
                    await app.alerts.send(
                        f"send_failed:{account.kind}", f"Не вдається надсилати в {account.kind}."
                    )
                events.append(
                    SendFailed(conversation.tenant_id, conversation.id, text, "помилка каналу")
                )
                continue
            message.external_id = ids[0] if ids else None
            message.delivery_status = "sent"
            await session.commit()
    return events


# --- notifications to masters --------------------------------------------------------------


async def member_chats(session, app: AppContext, tenant: Tenant) -> list[int]:
    if tenant.is_demo:
        chat = app.settings.demo_owner_chat_id
        return [chat] if chat else []
    rows = await session.scalars(
        select(TenantMember.telegram_user_id).where(
            TenantMember.tenant_id == tenant.id, TenantMember.notify.is_(True)
        )
    )
    return list(rows)


async def notify_members(
    app: AppContext,
    session,
    tenant: Tenant,
    cfg: StudioConfig,
    text: str,
    buttons: Keyboard | None = None,
) -> list[dict[str, int]]:
    silent = not in_owner_hours(cfg, app.clock.now())
    refs = []
    for chat_id in await member_chats(session, app, tenant):
        message_id = await app.notifier.send(chat_id, text, buttons=buttons, silent=silent)
        if message_id:
            refs.append({"chat_id": chat_id, "message_id": message_id})
    return refs


def _price(view) -> str:
    return price_text(view.price_type, view.price_min, view.price_max, view.currency, "uk")


def client_label(client: Client | None, booking: Booking | None = None) -> str:
    name = (booking.client_name if booking else None) or (client.name if client else None)
    return name or (client.display_name if client else None) or "Клієнтка"


def _when(cfg: StudioConfig, booking: Booking) -> str:
    if booking.start_at is None:
        return f"бажаний час: {booking.requested_time_text}"
    local = booking.start_at.astimezone(ZoneInfo(cfg.timezone))
    return f"{day_label(local.date(), 'uk')} о {hhmm(local)}"


async def _conversation_bits(session, conversation_id: int) -> tuple[Conversation, Client, str]:
    conversation = await session.get(Conversation, conversation_id)
    client = await session.get(Client, conversation.client_id)
    account = await session.get(ChannelAccount, conversation.channel_account_id)
    return conversation, client, CHANNEL_LABELS.get(account.kind, account.kind)


async def last_client_text(session, conversation_id: int) -> str:
    text = await session.scalar(
        select(Message.text)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == "client",
            Message.text.is_not(None),
        )
        .order_by(Message.id.desc())
        .limit(1)
    )
    text = text or "(без тексту)"
    return text if len(text) <= 300 else text[:297] + "…"


async def _booking_created(app, session, tenant, cfg, event: BookingCreated) -> None:
    booking = await session.get(Booking, event.booking_id)
    if booking is None or booking.is_sandbox:
        return
    view = await booking_view(session, booking, cfg)
    client = await session.get(Client, booking.client_id) if booking.client_id else None
    lines = [
        "🗓 Новий запис",
        f"{client_label(client, booking)} · {_when(cfg, booking)}",
        f"{view.service_name} · {_price(view)}",
    ]
    if view.staff_name and len(cfg.staff) > 1:
        lines.append(f"Майстер: {view.staff_name}")
    if booking.client_phone:
        lines.append(f"Телефон: {booking.client_phone}")
    buttons: Keyboard = [[Button("Скасувати запис", f"b:cancel:{booking.id}")]]
    if booking.status == "pending_prepayment" and view.prepayment_due:
        due = view.prepayment_due
        lines.append(
            f"⏳ Чекає на передоплату {money(view.prepayment_amount, view.currency, 'uk')} "
            f"до {day_label(due.date(), 'uk')} {hhmm(due)}."
        )
        buttons.insert(0, [Button("Оплату отримано", f"b:paid:{booking.id}")])
    else:
        lines.append("Записав бот. Нічого робити не треба.")
    if event.message_id:
        buttons.append([Button("👎 Відповідь бота", f"fb:{event.message_id}")])
    await notify_members(app, session, tenant, cfg, "\n".join(lines), buttons)


async def _booking_cancelled(app, session, tenant, cfg, event: BookingCancelled) -> None:
    booking = await session.get(Booking, event.booking_id)
    if booking is None or booking.is_sandbox:
        return
    client = await session.get(Client, booking.client_id) if booking.client_id else None
    view = await booking_view(session, booking, cfg)
    text = (
        f"❌ Клієнтка скасувала запис\n{client_label(client, booking)} · {_when(cfg, booking)}\n"
        f"{view.service_name}"
    )
    await notify_members(app, session, tenant, cfg, text)


async def _booking_rescheduled(app, session, tenant, cfg, event: BookingRescheduled) -> None:
    old = await session.get(Booking, event.old_booking_id)
    new = await session.get(Booking, event.new_booking_id)
    if new is None or new.is_sandbox:
        return
    client = await session.get(Client, new.client_id) if new.client_id else None
    view = await booking_view(session, new, cfg)
    text = (
        f"🔁 Клієнтка перенесла запис\n{client_label(client, new)} · {view.service_name}\n"
        f"Було: {_when(cfg, old)}\nСтало: {_when(cfg, new)}"
    )
    await notify_members(
        app, session, tenant, cfg, text, [[Button("Скасувати запис", f"b:cancel:{new.id}")]]
    )


async def _request_created(app, session, tenant, cfg, event: RequestCreated) -> None:
    booking = await session.get(Booking, event.booking_id)
    if booking is None or booking.is_sandbox:
        return
    client = await session.get(Client, booking.client_id) if booking.client_id else None
    view = await booking_view(session, booking, cfg)
    text = (
        f"📝 Нова заявка\n{client_label(client, booking)} · "
        f"{booking.client_phone or 'без телефону'}\n"
        f"{view.service_name}\nБажаний час: {booking.requested_time_text}"
    )
    buttons: Keyboard = [
        [Button("Підтвердити", f"r:ok:{booking.id}")],
        [Button("Інший час", f"r:other:{booking.id}"), Button("Відхилити", f"r:no:{booking.id}")],
    ]
    await notify_members(app, session, tenant, cfg, text, buttons)


async def _handoff(app, session, tenant, cfg, event: HandoffStarted) -> None:
    conversation, client, channel = await _conversation_bits(session, event.conversation_id)
    if conversation.is_sandbox:
        return
    last = await last_client_text(session, conversation.id)
    lines = [
        "🙋 Потрібна ваша відповідь",
        f"Причина: {REASON_LABELS.get(event.reason, event.reason)}",
    ]
    if event.summary:
        lines.append(event.summary)
    lines += [
        f"Клієнтка: {client_label(client)} ({channel})",
        f"Останнє повідомлення: «{last}»",
        "Бот замовк і чекає на вас.",
    ]
    if event.reason == "data_request":
        lines.append("Ми теж отримали цей запит і видалимо дані після вашої відповіді клієнтці.")
        for chat_id in app.settings.admin_telegram_ids:
            await app.notifier.send(
                chat_id,
                f"🔐 Запит на видалення даних: студія «{tenant.name}», клієнтка #{client.id} "
                f"({channel}). Коли майстер відповість: /forget {client.id}",
            )
    buttons: Keyboard = [
        [Button("Відповісти", f"c:reply:{conversation.id}")],
        [
            Button("Взяти діалог", f"c:take:{conversation.id}"),
            Button("Повернути бота", f"c:bot:{conversation.id}"),
        ],
    ]
    await notify_members(app, session, tenant, cfg, "\n".join(lines), buttons)


def draft_keyboard(draft: Draft) -> Keyboard:
    tool = (draft.proposed_action or {}).get("tool")
    if tool and tool != "handoff_to_human":
        # Side effects need an exact approval: no free-text edit of a booking confirmation.
        return [
            [Button("Відправити", f"d:send:{draft.id}")],
            [
                Button("Відповісти самій", f"c:reply:{draft.conversation_id}"),
                Button("Відхилити", f"d:no:{draft.id}"),
            ],
        ]
    return [
        [Button("Відправити", f"d:send:{draft.id}")],
        [Button("Редагувати", f"d:edit:{draft.id}"), Button("Відхилити", f"d:no:{draft.id}")],
    ]


async def draft_text(session, draft: Draft) -> str:
    _, client, channel = await _conversation_bits(session, draft.conversation_id)
    last = await last_client_text(session, draft.conversation_id)
    lines = [
        "✍️ Чернетка відповіді",
        f"{client_label(client)} ({channel}): «{last}»",
        "",
        "Бот пропонує:",
        draft.text,
    ]
    tool = (draft.proposed_action or {}).get("tool")
    if tool in ACTION_NOTES:
        lines += ["", ACTION_NOTES[tool]]
    return "\n".join(lines)


async def _draft_created(app, session, tenant, cfg, event: DraftCreated) -> None:
    draft = await session.get(Draft, event.draft_id)
    if draft is None or draft.status != "pending":
        return
    refs = await notify_members(
        app, session, tenant, cfg, await draft_text(session, draft), draft_keyboard(draft)
    )
    draft.owner_messages = refs
    await session.commit()


async def _media(app, session, tenant, cfg, event: MediaReceived) -> None:
    conversation, client, channel = await _conversation_bits(session, event.conversation_id)
    message = await session.get(Message, event.message_id)
    if conversation.is_sandbox or message is None:
        return
    name = MEDIA_NAMES.get(message.media_type or "", "вкладення")
    caption = f"📎 {client_label(client)} ({channel}) надіслала {name}"
    media = message.media or {}
    silent = not in_owner_hours(cfg, app.clock.now())
    for chat_id in await member_chats(session, app, tenant):
        if message.media_type == "photo" and (media.get("file_id") or media.get("url")):
            await app.notifier.send_photo(
                chat_id, media.get("file_id") or media["url"], caption, silent=silent
            )
        else:
            url = media.get("url")
            await app.notifier.send(chat_id, f"{caption}{': ' + url if url else ''}", silent=silent)


async def _send_failed(app, session, tenant, cfg, event: SendFailed) -> None:
    conversation, client, channel = await _conversation_bits(session, event.conversation_id)
    if conversation.is_sandbox:
        return
    contact = client.phone or client.display_name or client.external_user_id
    text = (
        f"⚠️ Не вдалося надіслати відповідь ({event.reason}). Напишіть клієнтці самі.\n"
        f"Клієнтка: {client_label(client)} ({channel}), контакт: {contact}\n"
        f"Текст: «{event.text}»"
    )
    await notify_members(app, session, tenant, cfg, text)


_HANDLERS = {
    BookingCreated: _booking_created,
    BookingCancelled: _booking_cancelled,
    BookingRescheduled: _booking_rescheduled,
    RequestCreated: _request_created,
    HandoffStarted: _handoff,
    DraftCreated: _draft_created,
    MediaReceived: _media,
    SendFailed: _send_failed,
}


async def dispatch_events(app: AppContext, events: list[Event]) -> None:
    for event in events:
        try:
            async with app.session_factory() as session:
                tenant = await session.get(Tenant, event.tenant_id)
                if tenant is None:
                    continue
                cfg = studio_config(tenant)
                await _HANDLERS[type(event)](app, session, tenant, cfg, event)
        except Exception:
            # A failed notification must not lose the client's reply; founders get an alert.
            log.exception("notify.failed", event=type(event).__name__)
            await app.alerts.send(
                f"notify:{type(event).__name__}", "Не вдалося надіслати сповіщення майстру."
            )


# --- founders ----------------------------------------------------------------------------------

LEAD_SERVICES = {
    "ai_admin": "AI-адміністратор",
    "voice": "AI-асистент для дзвінків",
    "crm": "CRM",
    "website": "сайт з онлайн-записом",
    "integration": "інтеграція",
    "other": "інше",
}


async def notify_lead(app: AppContext, lead_id: int) -> None:
    """A landing page request goes to every founder."""
    from app.db.models import Lead

    async with app.session_factory() as session:
        lead = await session.get(Lead, lead_id)
    if lead is None:
        return
    lines = [
        f"🔥 Нова заявка з сайту #{lead.id}",
        f"Ім'я: {lead.name}",
        f"Контакт: {lead.contact}",
    ]
    if lead.business:
        lines.append(f"Бізнес: {lead.business}")
    lines.append(f"Цікавить: {LEAD_SERVICES.get(lead.service or 'other', lead.service)}")
    if lead.message:
        lines.append(f"Повідомлення: {lead.message}")
    if lead.source:
        lines.append(f"Джерело: {lead.source}")
    lines.append("Статус заявок: /admin → Заявки")
    for chat_id in app.settings.admin_telegram_ids:
        await app.notifier.send(chat_id, "\n".join(lines))
