from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.main import create_app
from app.config import Settings
from app.db.models import Booking, Service, Staff, Tenant
from app.db.session import SessionFactory
from app.tenants.loader import load_tenant
from tests.helpers import demo_config, kyiv


def test_health_endpoint(database_url: str) -> None:
    app = create_app(Settings(database_url=database_url))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True}


async def test_load_tenant_is_versioned_and_keeps_runtime_state(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        tenant = await load_tenant(session, demo_config(), source="test")
        tenant.mode = "offhours"
        await session.commit()

        cfg = demo_config(name="Renamed Studio")
        await load_tenant(session, cfg, source="test")
        await session.commit()

        tenant = await session.scalar(select(Tenant))
        assert tenant.name == "Renamed Studio"
        assert tenant.config_version == 2
        assert tenant.mode == "offhours"  # owner's choice survives a config reload
        services = (await session.scalars(select(Service))).all()
        assert {s.key for s in services} >= {"gel_manicure", "gel_removal"}


async def _booking(session, ids, start, *, sandbox=False, key="k"):
    tenant_id, staff_id, service_id = ids
    booking = Booking(
        tenant_id=tenant_id,
        service_id=service_id,
        staff_id=staff_id,
        start_at=start,
        end_at=start + timedelta(minutes=120),
        blocked_until=start + timedelta(minutes=135),
        status="confirmed",
        price_type="fixed",
        price_min=900,
        price_max=900,
        currency="UAH",
        idempotency_key=key,
        is_sandbox=sandbox,
    )
    session.add(booking)
    await session.flush()
    return booking


async def test_exclusion_constraint_blocks_overlapping_bookings(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        tenant = await load_tenant(session, demo_config())
        staff = await session.scalar(select(Staff).where(Staff.key == "olena"))
        service = await session.scalar(select(Service).where(Service.key == "gel_manicure"))
        # Plain ids: a rollback below expires ORM objects.
        ids = (tenant.id, staff.id, service.id)
        await _booking(session, ids, kyiv("2026-10-21 15:00"), key="a")
        # Sandbox bookings never block real ones.
        await _booking(session, ids, kyiv("2026-10-21 15:30"), sandbox=True, key="b")
        await session.commit()

        with pytest.raises(IntegrityError, match="booking_no_overlap"):
            await _booking(session, ids, kyiv("2026-10-21 17:00"), key="c")
        await session.rollback()

        # Right after the buffer (15:00 + 120 + 15 min) is free again.
        await _booking(session, ids, kyiv("2026-10-21 17:15"), key="d")
        await session.commit()
