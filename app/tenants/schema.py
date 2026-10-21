"""Studio configuration (studios/*.json). Validated before it reaches the database or the bot.

Everything the bot may say about a studio comes from here (R2). `internal_notes` is never
shown to the model (R7).
"""

import itertools
from datetime import date, time
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

SUPPORTED_LANGUAGES = ("uk", "ru", "en", "pl")
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

Key = Annotated[str, Field(pattern=r"^[a-z0-9_]{2,64}$")]
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PriceType(StrEnum):
    FIXED = "fixed"
    FROM = "from"
    RANGE = "range"
    ON_REQUEST = "on_request"


class ServiceConfig(StrictModel):
    id: Key
    name: str = Field(min_length=2, max_length=200)
    aliases: list[str] = Field(default_factory=list)
    price_type: PriceType
    price_min: int | None = Field(default=None, ge=0)
    price_max: int | None = Field(default=None, ge=0)
    duration_min: int = Field(ge=5, le=720)
    buffer_min: int = Field(default=0, ge=0, le=240)
    prepayment_amount: int | None = Field(default=None, ge=0)
    description: str | None = None

    @model_validator(mode="after")
    def _check_price(self) -> "ServiceConfig":
        kind = self.price_type
        if kind is PriceType.FIXED:
            if self.price_min is None:
                raise ValueError(f"service {self.id}: fixed price needs price_min")
            if self.price_max is None:
                self.price_max = self.price_min
            elif self.price_max != self.price_min:
                raise ValueError(f"service {self.id}: fixed price needs price_max == price_min")
        elif kind is PriceType.FROM:
            if self.price_min is None or self.price_max is not None:
                raise ValueError(f"service {self.id}: 'from' price needs only price_min")
        elif kind is PriceType.RANGE:
            if self.price_min is None or self.price_max is None:
                raise ValueError(f"service {self.id}: range price needs price_min and price_max")
            if self.price_min > self.price_max:
                raise ValueError(f"service {self.id}: price_min is greater than price_max")
        elif self.price_min is not None or self.price_max is not None:
            raise ValueError(f"service {self.id}: on_request price must not have amounts")
        return self


class StaffConfig(StrictModel):
    id: Key
    name: str = Field(min_length=1, max_length=200)
    services: list[Key] = Field(min_length=1)
    google_calendar_id: str | None = None


class TimeRange(StrictModel):
    start: time
    end: time

    @model_validator(mode="after")
    def _ordered(self) -> "TimeRange":
        if self.start >= self.end:
            raise ValueError(f"time range {self.start}-{self.end}: start must be before end")
        return self


def _check_no_overlap(ranges: list[TimeRange], where: str) -> list[TimeRange]:
    ordered = sorted(ranges, key=lambda r: r.start)
    for prev, nxt in itertools.pairwise(ordered):
        if nxt.start < prev.end:
            raise ValueError(f"{where}: time ranges overlap")
    return ordered


class WeeklyHours(StrictModel):
    mon: list[TimeRange] = Field(default_factory=list)
    tue: list[TimeRange] = Field(default_factory=list)
    wed: list[TimeRange] = Field(default_factory=list)
    thu: list[TimeRange] = Field(default_factory=list)
    fri: list[TimeRange] = Field(default_factory=list)
    sat: list[TimeRange] = Field(default_factory=list)
    sun: list[TimeRange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sorted(self) -> "WeeklyHours":
        for key in WEEKDAY_KEYS:
            setattr(self, key, _check_no_overlap(getattr(self, key), key))
        return self

    def for_weekday(self, weekday: int) -> list[TimeRange]:
        return getattr(self, WEEKDAY_KEYS[weekday])


class DateException(StrictModel):
    date: date
    closed: bool = False
    hours: list[TimeRange] | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _closed_or_hours(self) -> "DateException":
        if self.closed == (self.hours is not None):
            raise ValueError(f"exception {self.date}: set either closed=true or hours")
        if self.hours is not None:
            self.hours = _check_no_overlap(self.hours, str(self.date))
        return self


class Prepayment(StrictModel):
    required: bool = False
    default_amount: int | None = Field(default=None, ge=1)
    deadline_hours: int = Field(default=24, ge=1, le=168)
    # Sent verbatim by a code template, never retyped by the model.
    requisites_text: str = ""


class Cancellation(StrictModel):
    free_cancel_hours: int = Field(default=24, ge=0, le=168)
    late_policy_text: str = ""


class FaqItem(StrictModel):
    q: str = Field(min_length=2)
    a: str = Field(min_length=1)


class Tone(StrictModel):
    notes: str | None = None
    examples: list[str] = Field(default_factory=list)


class Profile(StrictModel):
    address: str = Field(min_length=3)
    how_to_find: str | None = None
    map_url: HttpUrl | None = None
    phone: str | None = None
    instagram_url: HttpUrl | None = None
    portfolio_url: HttpUrl | None = None
    payment_methods: list[str] = Field(default_factory=list)
    privacy_url: HttpUrl | None = None
    age_policy: str | None = None
    extra_info: list[str] = Field(default_factory=list)


class BookingSettings(StrictModel):
    min_lead_minutes: int = Field(default=120, ge=0, le=7 * 24 * 60)
    horizon_days: int = Field(default=30, ge=1, le=180)
    slot_step_minutes: int = Field(default=30, ge=5, le=120)
    max_slots_returned: int = Field(default=6, ge=1, le=12)
    max_active_bookings_per_client: int = Field(default=2, ge=1, le=10)
    require_phone: bool = True


class Limits(StrictModel):
    replies_per_hour: int = Field(default=20, ge=1, le=500)
    daily_token_budget: int = Field(default=300_000, ge=1_000)


class HandoffSettings(StrictModel):
    resume_hours: int = Field(default=12, ge=1, le=168)
    holding_interval_hours: int = Field(default=12, ge=1, le=72)
    owner_reminder_minutes: int = Field(default=30, ge=5, le=24 * 60)


class GoogleSettings(StrictModel):
    calendar_id: str | None = None
    # By default Google shows all-day events as "free"; treat them as closed days anyway.
    all_day_events_block: bool = True


class StudioConfig(StrictModel):
    slug: Slug
    name: str = Field(min_length=2, max_length=200)
    city: str = Field(min_length=2)
    timezone: str = "Europe/Kyiv"
    currency: str = "UAH"
    default_language: str = "uk"
    allowed_languages: list[str] = Field(default_factory=lambda: ["uk"])
    calendar_provider: Literal["internal", "google", "lead_capture"] = "internal"
    initial_mode: Literal["draft", "offhours", "autopilot"] = "draft"
    profile: Profile
    hours: WeeklyHours
    owner_hours: WeeklyHours | None = None
    breaks: list[TimeRange] = Field(default_factory=list)
    exceptions: list[DateException] = Field(default_factory=list)
    services: list[ServiceConfig] = Field(min_length=1)
    staff: list[StaffConfig] = Field(min_length=1)
    prepayment: Prepayment = Field(default_factory=Prepayment)
    cancellation: Cancellation = Field(default_factory=Cancellation)
    faq: list[FaqItem] = Field(default_factory=list)
    tone: Tone = Field(default_factory=Tone)
    booking: BookingSettings = Field(default_factory=BookingSettings)
    limits: Limits = Field(default_factory=Limits)
    handoff: HandoffSettings = Field(default_factory=HandoffSettings)
    google: GoogleSettings = Field(default_factory=GoogleSettings)
    disclosure_silence_days: int = Field(default=30, ge=1, le=365)
    retention_days: int = Field(default=90, ge=7, le=730)
    internal_notes: str | None = None

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> "StudioConfig":
        langs = self.allowed_languages
        unknown = [lang for lang in langs if lang not in SUPPORTED_LANGUAGES]
        if unknown:
            raise ValueError(f"unsupported languages {unknown}; supported: {SUPPORTED_LANGUAGES}")
        if self.default_language not in langs:
            raise ValueError("default_language must be one of allowed_languages")

        service_ids = [s.id for s in self.services]
        staff_ids = [s.id for s in self.staff]
        for label, ids in (("service", service_ids), ("staff", staff_ids)):
            duplicates = {i for i in ids if ids.count(i) > 1}
            if duplicates:
                raise ValueError(f"duplicate {label} ids: {sorted(duplicates)}")
        for member in self.staff:
            missing = [s for s in member.services if s not in service_ids]
            if missing:
                raise ValueError(f"staff {member.id} refers to unknown services {missing}")
        if self.calendar_provider != "lead_capture":
            covered = {s for member in self.staff for s in member.services}
            uncovered = [s for s in service_ids if s not in covered]
            if uncovered:
                raise ValueError(f"no staff member provides services {uncovered}")
        if self.calendar_provider == "google":
            without = [
                m.id for m in self.staff if not (m.google_calendar_id or self.google.calendar_id)
            ]
            if without:
                raise ValueError(f"google calendar id missing for staff {without}")

        if self.prepayment.required:
            if not self.prepayment.requisites_text.strip():
                raise ValueError("prepayment.required needs prepayment.requisites_text")
            no_amount = [
                s.id
                for s in self.services
                if s.prepayment_amount is None and self.prepayment.default_amount is None
            ]
            if no_amount:
                raise ValueError(f"prepayment amount missing for services {no_amount}")

        dates = [e.date for e in self.exceptions]
        if len(dates) != len(set(dates)):
            raise ValueError("duplicate dates in exceptions")
        self.breaks = _check_no_overlap(self.breaks, "breaks")
        return self

    # --- helpers ---

    @property
    def effective_owner_hours(self) -> WeeklyHours:
        return self.owner_hours or self.hours

    def service(self, key: str) -> ServiceConfig | None:
        return next((s for s in self.services if s.id == key), None)

    def staff_member(self, key: str) -> StaffConfig | None:
        return next((m for m in self.staff if m.id == key), None)

    def staff_for_service(self, key: str) -> list[StaffConfig]:
        return [m for m in self.staff if key in m.services]

    def exception_for(self, day: date) -> DateException | None:
        return next((e for e in self.exceptions if e.date == day), None)

    def prepayment_for(self, service: ServiceConfig) -> int | None:
        if not self.prepayment.required:
            return None
        return service.prepayment_amount or self.prepayment.default_amount
