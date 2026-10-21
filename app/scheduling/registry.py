from app.scheduling.base import CalendarProvider, CalendarRegistry
from app.scheduling.internal import InternalCalendar
from app.scheduling.lead_capture import LeadCaptureCalendar
from app.tenants.schema import StudioConfig


def default_calendars() -> CalendarRegistry:
    """Providers that need no credentials. Google is added at startup when configured."""
    return {"internal": InternalCalendar(), "lead_capture": LeadCaptureCalendar()}


def provider_for(registry: CalendarRegistry, cfg: StudioConfig) -> CalendarProvider:
    try:
        return registry[cfg.calendar_provider]
    except KeyError as exc:
        raise RuntimeError(
            f"calendar provider {cfg.calendar_provider!r} is not configured"
        ) from exc
