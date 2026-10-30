"""The worker's pulse, the /health worker status and the watchdog's one-time alerts."""

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.main import create_app
from app.config import Settings
from app.core import heartbeat
from app.core.heartbeat import WorkerWatchdog, beat, worker_age_s, worker_state


async def age_the_pulse(session_factory, seconds: int) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE job_run SET ran_at = now() - make_interval(secs => :s) "
                "WHERE name = 'heartbeat'"
            ),
            {"s": seconds},
        )


async def test_pulse_states(session_factory) -> None:
    assert await worker_state(session_factory) == "unknown"
    assert await worker_age_s(session_factory) is None
    await beat(session_factory)
    await beat(session_factory)  # an upsert: still one row
    assert await worker_state(session_factory) == "ok"
    await age_the_pulse(session_factory, heartbeat.STALE_AFTER_S + 10)
    assert await worker_state(session_factory) == "stale"
    await beat(session_factory)
    assert await worker_state(session_factory) == "ok"
    async with session_factory() as session:
        count = await session.scalar(text("SELECT count(*) FROM job_run WHERE name = 'heartbeat'"))
    assert count == 1


async def test_watchdog_alerts_once_and_reports_recovery(session_factory) -> None:
    sent: list[str] = []

    async def notify(message: str) -> None:
        sent.append(message)

    watchdog = WorkerWatchdog(session_factory, notify)
    await watchdog.check()  # the worker never ran: nothing to report
    assert sent == []
    await beat(session_factory)
    await watchdog.check()
    assert sent == []
    await age_the_pulse(session_factory, heartbeat.STALE_AFTER_S + 10)
    await watchdog.check()
    await watchdog.check()  # still down: no second alert
    assert len(sent) == 1 and "Воркер не відповідає" in sent[0]
    await beat(session_factory)
    await watchdog.check()
    assert len(sent) == 2 and sent[1] == "Воркер знову працює."


async def test_health_reports_a_stale_worker(session_factory, database_url: str) -> None:
    await beat(session_factory)
    app = create_app(Settings(_env_file=None, database_url=database_url))
    with TestClient(app) as client:
        assert client.get("/health").json()["worker"] == "ok"
    await age_the_pulse(session_factory, heartbeat.STALE_AFTER_S + 10)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": True, "worker": "stale"}
