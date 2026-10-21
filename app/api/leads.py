"""Landing page form: a studio leaves a request, founders get it in Telegram (via the worker).

Spam protection without captchas: a hidden honeypot field, length limits, and a per-IP and
global rate limit kept in memory (one API process at our scale).
"""

import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.inbox import store_event
from app.db.models import Lead

router = APIRouter()

PER_IP = 5  # requests per hour from one address
GLOBAL = 60  # requests per hour in total
WINDOW_S = 3600
SERVICES = {"ai_admin", "voice", "crm", "website", "integration", "other"}


class LeadIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    contact: str = Field(min_length=3, max_length=200)
    business: str | None = Field(default=None, max_length=200)
    service: str | None = Field(default=None, max_length=64)
    message: str | None = Field(default=None, max_length=2000)
    source: str | None = Field(default=None, max_length=64)
    website: str | None = None  # honeypot: people never see this field, bots fill it


class RateLimiter:
    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, now: float) -> bool:
        queue = self.hits[key]
        while queue and queue[0] < now - WINDOW_S:
            queue.popleft()
        if len(queue) >= limit:
            return False
        queue.append(now)
        return True


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/leads", status_code=201)
async def create_lead(request: Request, body: LeadIn) -> dict[str, bool]:
    if body.website:
        return {"ok": True}  # pretend success so the bot does not retry
    limiter: RateLimiter = request.app.state.lead_limiter
    now = time.monotonic()
    if not limiter.allow("*", GLOBAL, now) or not limiter.allow(_client_ip(request), PER_IP, now):
        raise HTTPException(status_code=429, detail="too many requests, try later")
    service = body.service if body.service in SERVICES else "other"
    async with request.app.state.session_factory() as session, session.begin():
        lead = Lead(
            name=body.name.strip(),
            contact=body.contact.strip(),
            business=(body.business or "").strip() or None,
            service=service,
            message=(body.message or "").strip() or None,
            source=(body.source or "").strip() or None,
            status="new",
        )
        session.add(lead)
        await session.flush()
        await store_event(session, "lead", f"lead-{lead.id}", {"lead_id": lead.id})
    return {"ok": True}
