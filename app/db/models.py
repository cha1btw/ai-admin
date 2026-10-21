"""Database schema. See CLAUDE.md, "Модель данных".

All timestamps are timezone-aware (UTC in the database). Personal data lives only in
client, booking (client_name, client_phone) and message.text; retention jobs clean them.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

TZ = DateTime(timezone=True)


def _created() -> Mapped[datetime]:
    return mapped_column(TZ, server_default=func.now())


# --- Tenants and configuration -------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # Runtime state changed from Owner Bot; a config reload does not touch it.
    mode: Mapped[str] = mapped_column(String(16), default="draft")  # draft|offhours|autopilot
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|paused
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    # Validated StudioConfig (app/tenants/schema.py) as JSON; source of truth for studio data.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class TenantConfigVersion(Base):
    __tablename__ = "tenant_config_version"
    __table_args__ = (UniqueConstraint("tenant_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _created()


class Service(Base):
    __tablename__ = "service"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    price_type: Mapped[str] = mapped_column(String(16))  # fixed|from|range|on_request
    price_min: Mapped[int | None] = mapped_column(Integer)
    price_max: Mapped[int | None] = mapped_column(Integer)
    duration_min: Mapped[int] = mapped_column(Integer)
    buffer_min: Mapped[int] = mapped_column(Integer, default=0)
    prepayment_amount: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Staff(Base):
    __tablename__ = "staff"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    google_calendar_id: Mapped[str | None] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class StaffService(Base):
    __tablename__ = "staff_service"

    staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff.id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[int] = mapped_column(
        ForeignKey("service.id", ondelete="CASCADE"), primary_key=True
    )


# --- Studio people (Owner Bot) ---------------------------------------------------------------


class OwnerAccount(Base):
    """A Telegram user who talks to Owner Bot (master, studio admin or founder)."""

    __tablename__ = "owner_account"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    active_tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenant.id", ondelete="SET NULL")
    )
    # Pending multi-step interaction, e.g. {"awaiting": "reply", "conversation_id": 5}.
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()


class TenantMember(Base):
    __tablename__ = "tenant_member"
    __table_args__ = (UniqueConstraint("tenant_id", "telegram_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    telegram_user_id: Mapped[int] = mapped_column(
        ForeignKey("owner_account.telegram_user_id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(16), default="owner")  # owner|admin
    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class LinkToken(Base):
    """One-time deep link that binds a Telegram account to a studio."""

    __tablename__ = "link_token"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16), default="owner")
    expires_at: Mapped[datetime] = mapped_column(TZ)
    used_at: Mapped[datetime | None] = mapped_column(TZ)
    used_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()


# --- Channels, clients, conversations ----------------------------------------------------------


class ChannelAccount(Base):
    """A connected inbox: Telegram Business connection, Instagram account, demo bot, CLI,
    or the sandbox."""

    __tablename__ = "channel_account"
    __table_args__ = (UniqueConstraint("kind", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(
        String(32)
    )  # cli|sandbox|telegram_demo|telegram_business|instagram
    external_id: Mapped[str] = mapped_column(String(128))
    credentials: Mapped[str | None] = mapped_column(Text)  # encrypted (app/core/crypto.py)
    token_expires_at: Mapped[datetime | None] = mapped_column(TZ)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|disabled
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class Client(Base):
    __tablename__ = "client"
    __table_args__ = (UniqueConstraint("channel_account_id", "external_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    channel_account_id: Mapped[int] = mapped_column(
        ForeignKey("channel_account.id", ondelete="CASCADE")
    )
    external_user_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(32))
    language: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)


class Conversation(Base):
    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    channel_account_id: Mapped[int] = mapped_column(
        ForeignKey("channel_account.id", ondelete="CASCADE")
    )
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id", ondelete="CASCADE"), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="BOT_ACTIVE")
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    last_client_message_at: Mapped[datetime | None] = mapped_column(TZ)
    last_bot_message_at: Mapped[datetime | None] = mapped_column(TZ)
    last_human_message_at: Mapped[datetime | None] = mapped_column(TZ)
    disclosed_at: Mapped[datetime | None] = mapped_column(TZ)
    handoff_reason: Mapped[str | None] = mapped_column(String(32))
    handoff_at: Mapped[datetime | None] = mapped_column(TZ)
    handoff_reminded_at: Mapped[datetime | None] = mapped_column(TZ)
    holding_sent_at: Mapped[datetime | None] = mapped_column(TZ)
    # Debounce: the worker replies once now >= reply_due_at.
    reply_due_at: Mapped[datetime | None] = mapped_column(TZ, index=True)
    last_processed_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    voice_strikes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class Message(Base):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "external_id"),
        Index("ix_message_conversation_id_id", "conversation_id", "id"),
        Index("ix_message_tenant_id_created_at", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(8))  # client|bot|human|system
    external_id: Mapped[str | None] = mapped_column(String(128))
    text: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(16))  # photo|video|voice|sticker|file
    is_story_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    config_version: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(64))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    delivery_status: Mapped[str | None] = mapped_column(String(16))  # sent|failed|delivered
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class InboundEvent(Base):
    """Raw webhook or poll payload; the worker turns it into messages. Short retention (R9)."""

    __tablename__ = "inbound_event"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_inbound_event_status_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # telegram_owner|telegram_demo|instagram
    external_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(TZ)


# --- Booking -----------------------------------------------------------------------------------


class OfferedSlot(Base):
    """Opaque slot token shown to the model (R4). Only these tokens can be booked."""

    __tablename__ = "offered_slot"

    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id", ondelete="CASCADE"))
    service_id: Mapped[int] = mapped_column(ForeignKey("service.id", ondelete="CASCADE"))
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"))
    start_at: Mapped[datetime] = mapped_column(TZ)
    end_at: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ)
    expires_at: Mapped[datetime] = mapped_column(TZ)


ACTIVE_BOOKING_STATUSES = ("requested", "pending_prepayment", "confirmed")
SLOT_HOLDING_STATUSES = ("pending_prepayment", "confirmed")


class Booking(Base):
    """A booking or, for lead_capture studios, a request awaiting the master's confirmation.

    The migration also creates an exclusion constraint (booking_no_overlap): two bookings of
    one staff member with status pending_prepayment/confirmed cannot overlap [start_at,
    blocked_until). Sandbox bookings are excluded from it.
    """

    __tablename__ = "booking"
    __table_args__ = (
        CheckConstraint(
            "status NOT IN ('pending_prepayment', 'confirmed') OR start_at IS NOT NULL",
            name="slot_required",
        ),
        Index("ix_booking_tenant_id_start_at", "tenant_id", "start_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation.id", ondelete="SET NULL")
    )
    client_id: Mapped[int | None] = mapped_column(ForeignKey("client.id", ondelete="SET NULL"))
    service_id: Mapped[int] = mapped_column(ForeignKey("service.id"))
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    start_at: Mapped[datetime | None] = mapped_column(TZ)
    end_at: Mapped[datetime | None] = mapped_column(TZ)
    blocked_until: Mapped[datetime | None] = mapped_column(TZ)  # end_at plus buffer
    requested_time_text: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24))
    price_type: Mapped[str] = mapped_column(String(16))
    price_min: Mapped[int | None] = mapped_column(Integer)
    price_max: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))
    client_name: Mapped[str | None] = mapped_column(String(100))
    client_phone: Mapped[str | None] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(String(16), default="bot")  # bot|human
    external_event_id: Mapped[str | None] = mapped_column(String(300))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    prepayment_amount: Mapped[int | None] = mapped_column(Integer)
    prepayment_due_at: Mapped[datetime | None] = mapped_column(TZ)
    prepayment_confirmed_at: Mapped[datetime | None] = mapped_column(TZ)
    prepayment_reminded_at: Mapped[datetime | None] = mapped_column(TZ)
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class Block(Base):
    """Closed time in the internal calendar (/close): vacation, power outage, sick day."""

    __tablename__ = "block"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"))
    start_at: Mapped[datetime] = mapped_column(TZ)
    end_at: Mapped[datetime] = mapped_column(TZ)
    reason: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


# --- Owner control -----------------------------------------------------------------------------


class Draft(Base):
    """Reply prepared by the bot that waits for the master (draft mode, R12)."""

    __tablename__ = "draft"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    # Side effect staged until approval, e.g. {"tool": "create_booking", "args": {...}}.
    proposed_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    disclose: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    owner_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ)
    decided_at: Mapped[datetime | None] = mapped_column(TZ)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    message_id: Mapped[int | None] = mapped_column(ForeignKey("message.id", ondelete="SET NULL"))
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    verdict: Mapped[str] = mapped_column(String(16))  # down
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenant.id", ondelete="SET NULL"))
    actor: Mapped[str] = mapped_column(String(64))  # bot|system|owner:<tg id>|admin:<tg id>|cli
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()


class JobRun(Base):
    """Makes periodic jobs idempotent, e.g. one morning digest per studio per day."""

    __tablename__ = "job_run"
    __table_args__ = (UniqueConstraint("name", "scope", "run_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(64))
    run_key: Mapped[str] = mapped_column(String(64))
    ran_at: Mapped[datetime] = _created()
