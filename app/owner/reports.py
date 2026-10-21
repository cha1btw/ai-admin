"""Numbers about a studio's bot: /stats for masters, the Monday report that shows the bot's
value, and the founders' morning digest across all studios (with model cost)."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, literal_column, select

from app.brain.handoff import REASON_LABELS, in_owner_hours
from app.brain.i18n import money
from app.config import Settings
from app.context import AppContext
from app.db.models import (
    AuditLog,
    Booking,
    ChannelAccount,
    Conversation,
    Feedback,
    InboundEvent,
    Lead,
    Message,
    Tenant,
)
from app.tenants.loader import studio_config
from app.tenants.schema import StudioConfig

BOOKED = ("confirmed", "pending_prepayment")


@dataclass
class StudioStats:
    conversations: int
    bookings: int
    amount: int
    bookings_off_hours: int
    messages: int
    messages_off_hours: int
    first_reply: str
    handoffs: dict[str, int]
    downs: int
    failed: int
    tokens_in: int
    tokens_out: int

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


def token_cost(settings: Settings, tokens_in: int, tokens_out: int) -> float | None:
    """USD by the prices in .env (LLM_PRICE_*); None when they are not set."""
    if settings.llm_price_input_per_m is None or settings.llm_price_output_per_m is None:
        return None
    return (
        tokens_in * settings.llm_price_input_per_m + tokens_out * settings.llm_price_output_per_m
    ) / 1_000_000


async def collect_stats(session, tenant: Tenant, cfg: StudioConfig, since: datetime) -> StudioStats:
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
    booked = list(
        (
            await session.execute(
                select(Booking.created_at, Booking.price_min).where(
                    Booking.tenant_id == tenant.id,
                    Booking.created_by == "bot",
                    Booking.is_sandbox.is_(False),
                    Booking.created_at >= since,
                    Booking.status.in_(BOOKED),
                )
            )
        ).all()
    )
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
    tokens_in, tokens_out = (
        await session.execute(
            select(
                func.coalesce(func.sum(Message.tokens_in), 0),
                func.coalesce(func.sum(Message.tokens_out), 0),
            ).where(Message.tenant_id == tenant.id, Message.created_at >= since)
        )
    ).one()
    return StudioStats(
        conversations=conversations or 0,
        bookings=len(booked),
        amount=sum(price or 0 for _, price in booked),
        bookings_off_hours=sum(1 for created, _ in booked if not in_owner_hours(cfg, created)),
        messages=len(client_times),
        messages_off_hours=sum(1 for t in client_times if not in_owner_hours(cfg, t)),
        first_reply=await _median_first_reply(session, tenant.id, since),
        handoffs={r or "other": n for r, n in handoffs},
        downs=downs or 0,
        failed=failed or 0,
        tokens_in=int(tokens_in),
        tokens_out=int(tokens_out),
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


def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


def _handoff_text(stats: StudioStats) -> str:
    return (
        ", ".join(f"{REASON_LABELS.get(r, r)}: {n}" for r, n in stats.handoffs.items()) or "немає"
    )


async def stats_text(app: AppContext, tenant: Tenant, days: int, *, founders: bool = False) -> str:
    """/stats for masters; founders also see tokens and their cost."""
    cfg = studio_config(tenant)
    since = app.clock.now() - timedelta(days=days)
    async with app.session_factory() as session:
        s = await collect_stats(session, tenant, cfg, since)
    lines = [
        f"Статистика за {days} дн. ({tenant.name}):",
        f"Діалоги: {s.conversations}",
        f"Записи від бота: {s.bookings} на суму {money(s.amount, cfg.currency, 'uk')}",
        f"З них поза вашими годинами: {s.bookings_off_hours}",
        f"Повідомлення поза вашими годинами: {_share(s.messages_off_hours, s.messages)}",
        f"Медіана першої відповіді: {s.first_reply}",
        f"Передано вам: {_handoff_text(s)}",
        f"👎 на відповіді бота: {s.downs}",
        f"Не доставлено: {s.failed}",
    ]
    if founders:
        cost = token_cost(app.settings, s.tokens_in, s.tokens_out)
        lines.append(
            f"Токени моделі: {s.tokens}" + (f" (≈ ${cost:.2f})" if cost is not None else "")
        )
    return "\n".join(lines)


def weekly_report_text(tenant: Tenant, cfg: StudioConfig, s: StudioStats) -> str | None:
    """Monday message to masters: what the bot did for them last week."""
    if not s.conversations:
        return None
    lines = [f"📊 Тиждень AI-адміністратора «{tenant.name}»", f"Діалогів: {s.conversations}"]
    if s.bookings:
        lines.append(
            f"Записів від бота: {s.bookings} на суму {money(s.amount, cfg.currency, 'uk')}"
        )
        if s.bookings_off_hours:
            lines.append(f"З них поки ви відпочивали: {s.bookings_off_hours} 🌙")
    if s.messages:
        lines.append(
            f"Повідомлень поза вашими годинами: {_share(s.messages_off_hours, s.messages)}"
        )
    if s.first_reply != "—":
        lines.append(f"Бот відповідав у середньому за {s.first_reply}")
    lines.append(f"Передано вам: {_handoff_text(s)}")
    lines.append("Якщо бот десь відповів не так, натисніть 👎 під його відповіддю, ми виправимо.")
    return "\n".join(lines)


async def founders_digest_text(app: AppContext, now: datetime) -> str | None:
    """Morning digest for founders: every studio in one message, plus leads and problems."""
    since = now - timedelta(hours=24)
    lines = ["🧭 Зведення за добу"]
    total_in = total_out = 0
    async with app.session_factory() as session:
        tenants = list(
            await session.scalars(
                select(Tenant).where(Tenant.is_demo.is_(False)).order_by(Tenant.name)
            )
        )
        for tenant in tenants:
            cfg = studio_config(tenant)
            s = await collect_stats(session, tenant, cfg, since)
            total_in += s.tokens_in
            total_out += s.tokens_out
            flags = []
            if tenant.status == "paused":
                flags.append("⏸ пауза")
            if s.downs:
                flags.append(f"👎 {s.downs}")
            if s.failed:
                flags.append(f"⚠️ не доставлено {s.failed}")
            lines.append(
                f"• {tenant.name} ({tenant.mode}): діалогів {s.conversations}, записів "
                f"{s.bookings}, передач {sum(s.handoffs.values())}"
                + (f", {', '.join(flags)}" if flags else "")
            )
        leads = await session.scalar(
            select(func.count()).select_from(Lead).where(Lead.created_at >= since)
        )
        new_leads = await session.scalar(
            select(func.count()).select_from(Lead).where(Lead.status == "new")
        )
        failed_events = await session.scalar(
            select(func.count()).select_from(InboundEvent).where(InboundEvent.status == "failed")
        )
        expiring = list(
            await session.scalars(
                select(Tenant.name)
                .join(ChannelAccount, ChannelAccount.tenant_id == Tenant.id)
                .where(
                    ChannelAccount.token_expires_at.is_not(None),
                    ChannelAccount.token_expires_at < now + timedelta(days=7),
                )
            )
        )
    if not tenants and not leads:
        return None
    cost = token_cost(app.settings, total_in, total_out)
    lines.append(
        f"Токени: {total_in + total_out}" + (f" (≈ ${cost:.2f})" if cost is not None else "")
    )
    if leads or new_leads:
        lines.append(f"Заявки з сайту: нових за добу {leads}, необроблених {new_leads}")
    if failed_events:
        lines.append(f"⚠️ Подій з помилкою в черзі: {failed_events} (/health)")
    if expiring:
        lines.append(f"⚠️ Токен Instagram спливає за тиждень: {', '.join(expiring)}")
    return "\n".join(lines)
