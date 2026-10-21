"""Validate studio config files and load them into the database (versioned)."""

import json
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Service, Staff, StaffService, Tenant, TenantConfigVersion
from app.tenants.schema import StudioConfig


class ConfigError(Exception):
    pass


def read_config(path: Path) -> StudioConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read JSON: {exc}") from exc
    try:
        return StudioConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path}: invalid config:\n{exc}") from exc


def studio_config(tenant: Tenant) -> StudioConfig:
    """Parse the stored config. It was validated on load, so this only fails on manual DB edits."""
    return StudioConfig.model_validate(tenant.config)


async def load_tenant(
    session: AsyncSession, cfg: StudioConfig, *, source: str | None = None, is_demo: bool = False
) -> Tenant:
    """Create or update a tenant from a config. Runtime state (mode, status) is kept on update."""
    data = cfg.model_dump(mode="json")
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == cfg.slug))
    if tenant is None:
        tenant = Tenant(
            slug=cfg.slug,
            name=cfg.name,
            mode=cfg.initial_mode,
            status="active",
            is_demo=is_demo,
            config=data,
            config_version=1,
        )
        session.add(tenant)
        await session.flush()
    else:
        tenant.name = cfg.name
        tenant.config = data
        tenant.config_version += 1
        tenant.is_demo = tenant.is_demo or is_demo
    session.add(
        TenantConfigVersion(
            tenant_id=tenant.id, version=tenant.config_version, data=data, source=source
        )
    )

    services = {
        s.key: s
        for s in await session.scalars(select(Service).where(Service.tenant_id == tenant.id))
    }
    for sc in cfg.services:
        row = services.get(sc.id)
        if row is None:
            row = Service(tenant_id=tenant.id, key=sc.id)
            session.add(row)
            services[sc.id] = row
        row.name = sc.name
        row.price_type = sc.price_type.value
        row.price_min = sc.price_min
        row.price_max = sc.price_max
        row.duration_min = sc.duration_min
        row.buffer_min = sc.buffer_min
        row.prepayment_amount = cfg.prepayment_for(sc)
        row.active = True
    configured = {s.id for s in cfg.services}
    for key, row in services.items():
        if key not in configured:
            row.active = False  # kept for old bookings

    staff_rows = {
        m.key: m for m in await session.scalars(select(Staff).where(Staff.tenant_id == tenant.id))
    }
    for mc in cfg.staff:
        row = staff_rows.get(mc.id)
        if row is None:
            row = Staff(tenant_id=tenant.id, key=mc.id)
            session.add(row)
            staff_rows[mc.id] = row
        row.name = mc.name
        row.google_calendar_id = mc.google_calendar_id or cfg.google.calendar_id
        row.active = True
    configured_staff = {m.id for m in cfg.staff}
    for key, row in staff_rows.items():
        if key not in configured_staff:
            row.active = False
    await session.flush()

    staff_ids = [row.id for row in staff_rows.values()]
    await session.execute(delete(StaffService).where(StaffService.staff_id.in_(staff_ids)))
    for mc in cfg.staff:
        for service_key in mc.services:
            session.add(
                StaffService(staff_id=staff_rows[mc.id].id, service_id=services[service_key].id)
            )
    await session.flush()
    return tenant
