"""HTTP entry point: health check and webhooks. Webhooks only store events (fast 200);
the worker does the rest (see docs/PRODUCT.md, section 1)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import make_engine, make_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        engine = make_engine(settings.database_url)
        app.state.settings = settings
        app.state.session_factory = make_session_factory(engine)
        yield
        await engine.dispose()

    app = FastAPI(title="AI-admin", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.include_router(health.router)
    return app


app = create_app()
