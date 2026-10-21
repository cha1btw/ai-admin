"""Client-facing texts built by code from database values (R3)."""

from app.brain.booking import BookingView
from app.brain.i18n import day_label, hhmm, money, price_text
from app.brain.templates import master_suffix, render
from app.tenants.schema import StudioConfig


def _values(cfg: StudioConfig, view: BookingView, lang: str) -> dict[str, str]:
    start = view.start
    return {
        "name": view.client_name or "",
        "service": view.service_name,
        "day": day_label(start.date(), lang) if start else "",
        "time": hhmm(start) if start else "",
        "price": price_text(view.price_type, view.price_min, view.price_max, view.currency, lang),
        "address": cfg.profile.address,
        "master": master_suffix(view.staff_name if len(cfg.staff) > 1 else None, lang),
    }


def booking_text(cfg: StudioConfig, view: BookingView, lang: str) -> str:
    values = _values(cfg, view, lang)
    if view.status == "pending_prepayment" and view.prepayment_amount and view.prepayment_due:
        due = view.prepayment_due
        return (
            render("booking_reserved", lang, **values)
            + " "
            + render(
                "prepayment_request",
                lang,
                amount=money(view.prepayment_amount, view.currency, lang),
                deadline=f"{day_label(due.date(), lang)} {hhmm(due)}",
                requisites=cfg.prepayment.requisites_text.strip(),
            )
        )
    return render("booking_confirmed", lang, **values)


def cancelled_text(cfg: StudioConfig, view: BookingView, lang: str) -> str:
    if view.start is None:
        return render("request_cancelled", lang)
    values = _values(cfg, view, lang)
    return render("booking_cancelled", lang, day=values["day"], time=values["time"])


def rescheduled_text(cfg: StudioConfig, view: BookingView, lang: str) -> str:
    values = _values(cfg, view, lang)
    return render(
        "booking_rescheduled",
        lang,
        service=values["service"],
        day=values["day"],
        time=values["time"],
        address=values["address"],
    )


def request_text(view: BookingView, lang: str, eta: str) -> str:
    return render(
        "request_received",
        lang,
        name=view.client_name or "",
        service=view.service_name,
        wish=view.requested_time_text or "",
        eta=eta,
    )


def request_confirmed_text(cfg: StudioConfig, view: BookingView, lang: str) -> str:
    values = _values(cfg, view, lang)
    return render(
        "request_confirmed",
        lang,
        service=values["service"],
        day=values["day"],
        time=values["time"],
        address=values["address"],
    )


def prepayment_confirmed_text(cfg: StudioConfig, view: BookingView, lang: str) -> str:
    values = _values(cfg, view, lang)
    return render(
        "prepayment_confirmed",
        lang,
        service=values["service"],
        day=values["day"],
        time=values["time"],
    )
