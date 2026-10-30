"""Is the worker alive? It writes a timestamp every few seconds; the API and the watchdog read it.

A dead worker is a silent failure: webhooks still return 200 and events pile up, but nobody
answers clients. Stored in job_run (one upserted row), so no migration is needed. Both sides use
the database clock, so a skewed host clock cannot fake a stale or a fresh pulse.
"""

from collections.abc import Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.logging import get_logger
from app.db.models import JobRun
from app.db.session import SessionFactory

log = get_logger(__name__)

NAME, SCOPE, KEY = "heartbeat", "worker", "latest"
BEAT_EVERY_S = 15
STALE_AFTER_S = 90


async def beat(factory: SessionFactory) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            insert(JobRun)
            .values(name=NAME, scope=SCOPE, run_key=KEY)
            .on_conflict_do_update(
                index_elements=["name", "scope", "run_key"], set_={"ran_at": func.now()}
            )
        )


async def worker_age_s(factory: SessionFactory) -> float | None:
    """Seconds since the last pulse; None if the worker never ran against this database."""
    async with factory() as session:
        age = await session.scalar(
            select(func.extract("epoch", func.now() - JobRun.ran_at)).where(
                JobRun.name == NAME, JobRun.scope == SCOPE, JobRun.run_key == KEY
            )
        )
    return None if age is None else float(age)


async def worker_state(factory: SessionFactory) -> str:
    """ok, stale (it ran before and went quiet) or unknown (it never ran)."""
    age = await worker_age_s(factory)
    if age is None:
        return "unknown"
    return "stale" if age > STALE_AFTER_S else "ok"


class WorkerWatchdog:
    """Tells founders once when the worker goes quiet and once when it is back."""

    def __init__(self, factory: SessionFactory, notify: Callable[[str], Awaitable[None]]) -> None:
        self._factory = factory
        self._notify = notify
        self.down = False

    async def check(self) -> None:
        state = await worker_state(self._factory)
        if state == "stale" and not self.down:
            self.down = True
            await self._notify(
                "Воркер не відповідає: бот не обробляє повідомлення клієнток. "
                "Перевірте, чи працює `make worker`."
            )
        elif state == "ok" and self.down:
            self.down = False
            await self._notify("Воркер знову працює.")
