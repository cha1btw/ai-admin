"""The public projection of a studio config: exactly what the model may know (R2, R7).

No internal notes, no payment requisites (code sends them), no limits or owner hours.
"""

from datetime import date, timedelta
from typing import Any

from app.brain.i18n import WEEKDAYS, day_label, price_text
from app.tenants.schema import WEEKDAY_KEYS, StudioConfig, TimeRange


def _ranges(ranges: list[TimeRange]) -> str:
    if not ranges:
        return "вихідний"
    return ", ".join(f"{r.start:%H:%M}–{r.end:%H:%M}" for r in ranges)


def public_studio_data(cfg: StudioConfig, today: date) -> dict[str, Any]:
    horizon = today + timedelta(days=cfg.booking.horizon_days)
    staff_by_service = {s.id: [m.name for m in cfg.staff_for_service(s.id)] for s in cfg.services}

    data: dict[str, Any] = {
        "studio": {
            "name": cfg.name,
            "city": cfg.city,
            "address": cfg.profile.address,
            "how_to_find": cfg.profile.how_to_find,
            "map_url": str(cfg.profile.map_url) if cfg.profile.map_url else None,
            "phone": cfg.profile.phone,
            "instagram_url": str(cfg.profile.instagram_url) if cfg.profile.instagram_url else None,
            "portfolio_url": str(cfg.profile.portfolio_url) if cfg.profile.portfolio_url else None,
        },
        "working_hours": {
            WEEKDAYS["uk"][i]: _ranges(cfg.hours.for_weekday(i)) for i in range(len(WEEKDAY_KEYS))
        },
        "breaks": [f"{b.start:%H:%M}–{b.end:%H:%M}" for b in cfg.breaks],
        "special_days": [
            {
                "date": day_label(e.date, "uk"),
                "hours": "вихідний" if e.closed else _ranges(e.hours or []),
                "note": e.note,
            }
            for e in sorted(cfg.exceptions, key=lambda e: e.date)
            if today <= e.date <= horizon
        ],
        "services": [
            {
                "id": s.id,
                "name": s.name,
                "aliases": s.aliases,
                "price_type": s.price_type.value,
                "price": price_text(s.price_type, s.price_min, s.price_max, cfg.currency, "uk"),
                "duration_min": s.duration_min,
                "description": s.description,
                "masters": staff_by_service.get(s.id, []),
            }
            for s in cfg.services
        ],
        "masters": [{"id": m.id, "name": m.name, "services": m.services} for m in cfg.staff],
        "payment_methods": cfg.profile.payment_methods,
        "prepayment": {
            "required": cfg.prepayment.required,
            "deadline_hours": cfg.prepayment.deadline_hours if cfg.prepayment.required else None,
        },
        "cancellation": {
            "free_cancel_hours": cfg.cancellation.free_cancel_hours,
            "rule": cfg.cancellation.late_policy_text or None,
        },
        "faq": [{"q": f.q, "a": f.a} for f in cfg.faq],
        "age_policy": cfg.profile.age_policy,
        "extra_info": cfg.profile.extra_info,
    }
    return data
