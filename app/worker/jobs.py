"""Periodic jobs. Each is idempotent, so running one twice does no harm."""

from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.brain.handoff import in_owner_hours, owner_ranges, return_to_bot
from app.brain.i18n import day_label, hhmm, money
from app.channels.base import window_open
from app.context import AppContext
from app.core.logging import get_logger
from app.db.models import (
    Booking,
    ChannelAccount,
    Client,
    Conversation,
    Draft,
    JobRun,
    Message,
    Staff,
    Tenant,
)
from app.owner.notifications import (
    CHANNEL_LABELS,
    client_label,
    last_client_text,
    notify_members,
)
from app.owner.notifier import Button
from app.owner.reports import collect_stats, founders_digest_text, weekly_report_text
from app.privacy.retention import apply_retention
from app.scheduling.base import CalendarError
from app.tenants.loader import studio_config

log = get_logger(__name__)

FOUNDERS_TZ = ZoneInfo("Europe/Kyiv")
FOUNDERS_DIGEST_HOUR = 9
WEEKLY_REPORT_FALLBACK = time(10, 0)  # masters who do not work on Mondays


class Scheduler:
    def __init__(self, app: AppContext) -> None:
        self.app = app
        self._last: dict[str, datetime] = {}

    async def _every(
        self, name: str, interval: timedelta, job: Callable[[], Awaitable[None]]
    ) -> None:
        now = self.app.clock.now()
        last = self._last.get(name)
        if last is not None and now - last < interval:
            return
        self._last[name] = now
        try:
            await job()
        except Exception:
            log.exception("job.failed", job=name)
            await self.app.alerts.send(
                f"job:{name}", f"Фонова задача {name} впала, перевірте логи."
            )

    async def tick(self) -> None:
        await self._every("minutely", timedelta(minutes=1), self.minutely)
        await self._every("hourly", timedelta(hours=1), self.hourly)

    async def minutely(self) -> None:
        app = self.app
        await handoff_reminders(app)
        await resume_human_conversations(app)
        await expire_drafts(app)
        await prepayment_reminders(app)
        await morning_digests(app)
        await weekly_reports(app)
        await founders_digest(app)

    async def hourly(self) -> None:
        await apply_retention(self.app)
        await demo_cleanup(self.app)
        await reconcile_google_bookings(self.app)
        if "instagram" in self.app.channels:
            from app.channels.instagram import refresh_tokens

            await refresh_tokens(self.app)


async def handoff_reminders(app: AppContext) -> None:
    """The master has not answered a handed-off client: one reminder in the master's hours."""
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        rows = list(
            await session.scalars(
                select(Conversation).where(
                    Conversation.state == "HANDOFF_PENDING",
                    Conversation.handoff_reminded_at.is_(None),
                    Conversation.handoff_at.is_not(None),
                    Conversation.is_sandbox.is_(False),
                )
            )
        )
        for conversation in rows:
            tenant = await session.get(Tenant, conversation.tenant_id)
            cfg = studio_config(tenant)
            waited = now - conversation.handoff_at
            if waited < timedelta(minutes=cfg.handoff.owner_reminder_minutes):
                continue
            if not in_owner_hours(cfg, now):
                continue  # the morning digest lists it instead
            client = await session.get(Client, conversation.client_id)
            account = await session.get(ChannelAccount, conversation.channel_account_id)
            last = await last_client_text(session, conversation.id)
            minutes = int(waited.total_seconds() // 60)
            text = (
                f"⏰ {client_label(client)} ({CHANNEL_LABELS.get(account.kind, account.kind)}) "
                f"чекає на вашу відповідь уже {minutes} хв.\nОстаннє повідомлення: «{last}»"
            )
            buttons = [
                [
                    Button("Відповісти", f"c:reply:{conversation.id}"),
                    Button("Повернути бота", f"c:bot:{conversation.id}"),
                ]
            ]
            await notify_members(app, session, tenant, cfg, text, buttons)
            conversation.handoff_reminded_at = now


async def resume_human_conversations(app: AppContext) -> None:
    """R8: the bot returns after resume_hours without messages from the master."""
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        rows = list(
            await session.scalars(select(Conversation).where(Conversation.state == "HUMAN_ACTIVE"))
        )
        for conversation in rows:
            tenant = await session.get(Tenant, conversation.tenant_id)
            hours = studio_config(tenant).handoff.resume_hours
            last = conversation.last_human_message_at or conversation.handoff_at
            if last is not None and now - last >= timedelta(hours=hours):
                return_to_bot(conversation)


async def expire_drafts(app: AppContext) -> None:
    """R11: a draft cannot be sent after the channel window closed; tell the master."""
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        drafts = list(await session.scalars(select(Draft).where(Draft.status == "pending")))
        for draft in drafts:
            conversation = await session.get(Conversation, draft.conversation_id)
            account = await session.get(ChannelAccount, conversation.channel_account_id)
            channel = app.channels.get(account.kind)
            if channel is None or window_open(channel, conversation, now):
                continue
            draft.status = "expired"
            client = await session.get(Client, conversation.client_id)
            contact = client.phone or client.display_name or client.external_user_id
            text = (
                "⌛ Чернетка застаріла: вікно відповіді закрилося. "
                f"Напишіть клієнтці самі: {contact}\n\n{draft.text}"
            )
            for ref in draft.owner_messages or []:
                await app.notifier.edit(ref["chat_id"], ref["message_id"], text)


async def prepayment_reminders(app: AppContext) -> None:
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        rows = list(
            await session.scalars(
                select(Booking).where(
                    Booking.status == "pending_prepayment",
                    Booking.prepayment_due_at <= now,
                    Booking.prepayment_reminded_at.is_(None),
                    Booking.is_sandbox.is_(False),
                )
            )
        )
        for booking in rows:
            tenant = await session.get(Tenant, booking.tenant_id)
            cfg = studio_config(tenant)
            local = booking.start_at.astimezone(ZoneInfo(cfg.timezone))
            text = (
                f"⏳ Минув термін передоплати: {booking.client_name or 'клієнтка'} · "
                f"{day_label(local.date(), 'uk')} о {hhmm(local)}, "
                f"{money(booking.prepayment_amount or 0, booking.currency, 'uk')}.\n"
                "Бот запис сам не скасовує."
            )
            buttons = [
                [Button("Оплату отримано", f"b:paid:{booking.id}")],
                [Button("Скасувати запис", f"b:cancel:{booking.id}")],
            ]
            await notify_members(app, session, tenant, cfg, text, buttons)
            booking.prepayment_reminded_at = now


async def morning_digests(app: AppContext) -> None:
    """Once a day when the master's hours start: what happened while they were away."""
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        tenants = list(
            await session.scalars(
                select(Tenant).where(Tenant.is_demo.is_(False), Tenant.status == "active")
            )
        )
        for tenant in tenants:
            cfg = studio_config(tenant)
            local = now.astimezone(ZoneInfo(cfg.timezone))
            ranges = owner_ranges(cfg, local.date())
            if not ranges or local.time() < ranges[0].start:
                continue
            claimed = await session.execute(
                insert(JobRun)
                .values(name="digest", scope=str(tenant.id), run_key=local.date().isoformat())
                .on_conflict_do_nothing(index_elements=["name", "scope", "run_key"])
            )
            if not claimed.rowcount:
                continue
            text = await _digest_text(session, tenant, cfg, now)
            if text:
                await notify_members(app, session, tenant, cfg, text)


async def _claim(session, name: str, scope: str, run_key: str) -> bool:
    claimed = await session.execute(
        insert(JobRun)
        .values(name=name, scope=scope, run_key=run_key)
        .on_conflict_do_nothing(index_elements=["name", "scope", "run_key"])
    )
    return bool(claimed.rowcount)


async def weekly_reports(app: AppContext) -> None:
    """Monday, when the master's hours start: what the bot did for them last week."""
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        tenants = list(
            await session.scalars(
                select(Tenant).where(Tenant.is_demo.is_(False), Tenant.status == "active")
            )
        )
        for tenant in tenants:
            cfg = studio_config(tenant)
            local = now.astimezone(ZoneInfo(cfg.timezone))
            ranges = owner_ranges(cfg, local.date())
            start = ranges[0].start if ranges else WEEKLY_REPORT_FALLBACK
            if local.weekday() != 0 or local.time() < start:
                continue
            year, week, _ = local.isocalendar()
            if not await _claim(session, "weekly", str(tenant.id), f"{year}-W{week:02d}"):
                continue
            stats = await collect_stats(session, tenant, cfg, now - timedelta(days=7))
            text = weekly_report_text(tenant, cfg, stats)
            if text:
                await notify_members(app, session, tenant, cfg, text)


async def founders_digest(app: AppContext) -> None:
    """Every morning at FOUNDERS_DIGEST_HOUR (Kyiv): all studios in one silent message."""
    if not app.settings.admin_telegram_ids:
        return
    now = app.clock.now()
    local = now.astimezone(FOUNDERS_TZ)
    if local.hour < FOUNDERS_DIGEST_HOUR:
        return
    async with app.session_factory() as session, session.begin():
        if not await _claim(session, "founders_digest", "", local.date().isoformat()):
            return
    text = await founders_digest_text(app, now)
    if text:
        for chat_id in app.settings.admin_telegram_ids:
            await app.notifier.send(chat_id, text, silent=True)


async def _digest_text(session, tenant: Tenant, cfg, now: datetime) -> str | None:
    since = now - timedelta(hours=24)
    created = list(
        await session.scalars(
            select(Booking)
            .where(
                Booking.tenant_id == tenant.id,
                Booking.created_by == "bot",
                Booking.is_sandbox.is_(False),
                Booking.created_at >= since,
                Booking.status.in_(("confirmed", "pending_prepayment", "requested")),
            )
            .order_by(Booking.created_at)
        )
    )
    waiting = await session.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(
            Conversation.tenant_id == tenant.id,
            Conversation.state == "HANDOFF_PENDING",
            Conversation.is_sandbox.is_(False),
        )
    )
    drafts = await session.scalar(
        select(func.count())
        .select_from(Draft)
        .where(Draft.tenant_id == tenant.id, Draft.status == "pending")
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
    if not (created or waiting or drafts or failed):
        return None
    tz = ZoneInfo(cfg.timezone)
    lines = ["☀️ Доброго ранку! За останню добу:"]
    if created:
        lines.append(f"Нових записів і заявок від бота: {len(created)}")
        for booking in created[:10]:
            when = (
                f"{day_label(booking.start_at.astimezone(tz).date(), 'uk')} о "
                f"{hhmm(booking.start_at.astimezone(tz))}"
                if booking.start_at
                else f"заявка: {booking.requested_time_text}"
            )
            lines.append(f"  • {booking.client_name or 'клієнтка'} · {when}")
    if waiting:
        lines.append(f"Чекають на вашу відповідь: {waiting}")
    if drafts:
        lines.append(f"Непогоджених чернеток: {drafts}")
    if failed:
        lines.append(f"Не вдалося доставити повідомлень: {failed}")
    return "\n".join(lines)


async def demo_cleanup(app: AppContext) -> None:
    """Public demo bookings are for testing: removed after a day; idle demo chats after a week.
    Admin panel test chats go after a day."""
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        test_accounts = select(ChannelAccount.id).where(ChannelAccount.kind == "admin_test")
        await session.execute(
            delete(Booking).where(
                Booking.is_sandbox.is_(True), Booking.created_at < now - timedelta(days=1)
            )
        )
        await session.execute(
            delete(Client).where(
                Client.channel_account_id.in_(test_accounts),
                Client.last_seen_at < now - timedelta(days=1),
            )
        )
        demo_ids = list(await session.scalars(select(Tenant.id).where(Tenant.is_demo.is_(True))))
        if not demo_ids:
            return
        await session.execute(
            delete(Booking).where(
                Booking.tenant_id.in_(demo_ids), Booking.created_at < now - timedelta(days=1)
            )
        )
        await session.execute(
            delete(Client).where(
                Client.tenant_id.in_(demo_ids), Client.last_seen_at < now - timedelta(days=7)
            )
        )


async def reconcile_google_bookings(app: AppContext) -> None:
    """The master deleted our event in Google Calendar: the slot is free again, so the booking is
    marked cancelled by the studio and the master is told (the bot does not write to the client)."""
    provider = app.calendars.get("google")
    if provider is None:
        return
    now = app.clock.now()
    async with app.session_factory() as session, session.begin():
        rows = list(
            await session.scalars(
                select(Booking).where(
                    Booking.status.in_(("confirmed", "pending_prepayment")),
                    Booking.external_event_id.is_not(None),
                    Booking.is_sandbox.is_(False),
                    Booking.start_at > now,
                    Booking.start_at < now + timedelta(days=14),
                )
            )
        )
        for booking in rows:
            tenant = await session.get(Tenant, booking.tenant_id)
            cfg = studio_config(tenant)
            if cfg.calendar_provider != "google" or booking.staff_id is None:
                continue
            staff = await session.get(Staff, booking.staff_id)
            try:
                exists = await provider.event_exists(cfg, staff, booking.external_event_id)
            except CalendarError:
                log.warning("reconcile.check_failed", booking_id=booking.id)
                continue
            if exists:
                continue
            booking.status = "cancelled_by_studio"
            booking.cancel_reason = "deleted_in_calendar"
            local = booking.start_at.astimezone(ZoneInfo(cfg.timezone))
            text = (
                f"🗑 Подію {booking.client_name or 'клієнтки'} · {day_label(local.date(), 'uk')} о "
                f"{hhmm(local)} видалено з Google Календаря. Запис позначено скасованим. "
                "Бот клієнтці не писав: повідомте її, якщо потрібно "
                f"({booking.client_phone or '—'})."
            )
            await notify_members(app, session, tenant, cfg, text)
