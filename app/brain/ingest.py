"""Inbound side: every channel turns its webhook payload into these calls.

Client messages only schedule a reply (debounce via reply_due_at); the reply itself is built by
the orchestrator. Messages the studio sends by hand switch the conversation to HUMAN_ACTIVE (R8).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain.handoff import take_over
from app.db.models import ChannelAccount, Client, Conversation, Draft, Message

MAX_TEXT = 4000
ECHO_MATCH_WINDOW = timedelta(minutes=5)


@dataclass
class Inbound:
    external_user_id: str
    external_message_id: str | None
    text: str | None
    media_type: str | None = None  # photo|video|voice|sticker|file
    media: dict[str, Any] | None = None
    is_story_reply: bool = False
    display_name: str | None = None
    language: str | None = None  # hint from the platform, e.g. Telegram language_code


async def get_or_create_conversation(
    session: AsyncSession,
    account: ChannelAccount,
    external_user_id: str,
    now: datetime,
    *,
    display_name: str | None = None,
    sandbox: bool = False,
) -> tuple[Client, Conversation]:
    client = await session.scalar(
        select(Client).where(
            Client.channel_account_id == account.id, Client.external_user_id == external_user_id
        )
    )
    if client is None:
        client = Client(
            tenant_id=account.tenant_id,
            channel_account_id=account.id,
            external_user_id=external_user_id,
            display_name=display_name,
            last_seen_at=now,
        )
        session.add(client)
        await session.flush()
        conversation = Conversation(
            tenant_id=account.tenant_id,
            channel_account_id=account.id,
            client_id=client.id,
            state="BOT_ACTIVE",
            is_sandbox=sandbox,
            last_processed_message_id=0,
            voice_strikes=0,
        )
        session.add(conversation)
        await session.flush()
        return client, conversation
    if display_name:
        client.display_name = display_name
    conversation = await session.scalar(
        select(Conversation).where(Conversation.client_id == client.id)
    )
    return client, conversation


async def _find_message(
    session: AsyncSession, conversation_id: int, external_id: str
) -> Message | None:
    return await session.scalar(
        select(Message).where(
            Message.conversation_id == conversation_id, Message.external_id == external_id
        )
    )


async def record_client_message(
    session: AsyncSession,
    account: ChannelAccount,
    inbound: Inbound,
    now: datetime,
    *,
    debounce: timedelta,
    sandbox: bool = False,
    schedule: bool = True,
) -> Message | None:
    """Store a client message and schedule a reply. Returns None for a duplicate delivery.

    schedule=False leaves the reply to the caller (the admin test chat answers inline)."""
    client, conversation = await get_or_create_conversation(
        session,
        account,
        inbound.external_user_id,
        now,
        display_name=inbound.display_name,
        sandbox=sandbox,
    )
    if inbound.external_message_id and await _find_message(
        session, conversation.id, inbound.external_message_id
    ):
        return None
    message = Message(
        tenant_id=account.tenant_id,
        conversation_id=conversation.id,
        role="client",
        external_id=inbound.external_message_id,
        text=(inbound.text or "").strip()[:MAX_TEXT] or None,
        media_type=inbound.media_type,
        media=inbound.media,
        is_story_reply=inbound.is_story_reply,
        created_at=now,
    )
    session.add(message)
    await session.flush()
    conversation.last_client_message_at = now
    if schedule:
        conversation.reply_due_at = now + debounce
    client.last_seen_at = now
    if client.language is None and inbound.language in ("uk", "ru", "en", "pl"):
        client.language = inbound.language
    return message


async def record_outgoing(
    session: AsyncSession,
    account: ChannelAccount,
    external_user_id: str,
    external_message_id: str | None,
    text: str | None,
    now: datetime,
    *,
    from_bot: bool = False,
) -> Conversation | None:
    """A message sent from the studio's account (Instagram echo, Telegram business message
    written by the owner). Our own replies are only marked delivered; anything else means a
    human is writing, so the bot steps back (R8). Returns the conversation if a human took over."""
    _, conversation = await get_or_create_conversation(session, account, external_user_id, now)
    if external_message_id:
        own = await _find_message(session, conversation.id, external_message_id)
        if own is not None:
            if own.role == "bot":
                own.delivery_status = "delivered"
            return None
    # The echo of a multi-part reply, or an echo racing our database write: match by text.
    if text:
        recent_bot = await session.scalars(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.role == "bot",
                Message.created_at >= now - ECHO_MATCH_WINDOW,
            )
        )
        needle = " ".join(text.split())
        for message in recent_bot:
            if message.text and needle and needle in " ".join(message.text.split()):
                return None
    if from_bot:
        return None  # sent by a bot on the studio's behalf: never a human takeover

    session.add(
        Message(
            tenant_id=account.tenant_id,
            conversation_id=conversation.id,
            role="human",
            external_id=external_message_id,
            text=(text or "").strip()[:MAX_TEXT] or None,
            created_at=now,
        )
    )
    take_over(conversation, now)
    conversation.reply_due_at = None
    await supersede_drafts(session, conversation.id)
    await session.flush()
    return conversation


async def supersede_drafts(session: AsyncSession, conversation_id: int) -> list[Draft]:
    drafts = list(
        await session.scalars(
            select(Draft).where(Draft.conversation_id == conversation_id, Draft.status == "pending")
        )
    )
    for draft in drafts:
        draft.status = "superseded"
    return drafts


async def record_edit(
    session: AsyncSession,
    account: ChannelAccount,
    external_user_id: str,
    external_message_id: str,
    text: str | None,
    now: datetime,
) -> None:
    """An edit before the bot answered changes the pending message; later edits are ignored."""
    _, conversation = await get_or_create_conversation(session, account, external_user_id, now)
    message = await _find_message(session, conversation.id, external_message_id)
    if message is None or message.role != "client":
        return
    if message.id > conversation.last_processed_message_id:
        message.text = (text or "").strip()[:MAX_TEXT] or None


async def record_deleted(
    session: AsyncSession, account: ChannelAccount, external_user_id: str, external_ids: list[str]
) -> None:
    """The client deleted messages: drop their content (R9) and keep a marker."""
    client = await session.scalar(
        select(Client).where(
            Client.channel_account_id == account.id, Client.external_user_id == external_user_id
        )
    )
    if client is None:
        return
    conversation = await session.scalar(
        select(Conversation).where(Conversation.client_id == client.id)
    )
    if conversation is None or not external_ids:
        return
    await session.execute(
        update(Message)
        .where(Message.conversation_id == conversation.id, Message.external_id.in_(external_ids))
        .values(is_deleted=True, text=None, media=None)
    )
