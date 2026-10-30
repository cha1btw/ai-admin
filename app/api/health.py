from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.heartbeat import worker_state

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    try:
        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "degraded", "db": False}, status_code=503)
    # A dead worker is an outage too: webhooks still answer 200 while nobody replies to clients.
    # "unknown" (never ran, e.g. a fresh database or a test) is not a failure.
    worker = await worker_state(request.app.state.session_factory)
    if worker == "stale":
        return JSONResponse({"status": "degraded", "db": True, "worker": worker}, status_code=503)
    return JSONResponse({"status": "ok", "db": True, "worker": worker})
