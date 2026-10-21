"""Deleting a client's personal data on request (R9, "Видаліть мої дані").

Bookings stay for the studio's records but lose the name, phone and link to the person.
The client, the conversation, messages, drafts and offered slots are deleted (DB cascades).
"""

from sqlalchemy import update

from app.context import AppContext
from app.db.models import AuditLog, Booking, Client


async def forget_client(app: AppContext, client_id: int, *, actor: str) -> bool:
    async with app.session_factory() as session, session.begin():
        client = await session.get(Client, client_id)
        if client is None:
            return False
        tenant_id = client.tenant_id
        await session.execute(
            update(Booking)
            .where(Booking.client_id == client_id)
            .values(client_name=None, client_phone=None, client_id=None, conversation_id=None)
        )
        await session.delete(client)
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor,
                action="client.forgotten",
                details={"client_id": client_id},
            )
        )
    return True
