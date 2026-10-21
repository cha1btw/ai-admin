"""Language-aware formatting of dates, weekdays and prices for uk, ru, en, pl."""

from datetime import date, datetime

LANGS = ("uk", "ru", "en", "pl")

WEEKDAYS: dict[str, tuple[str, ...]] = {
    "uk": ("понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"),
    "ru": ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "pl": ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"),
}

_CURRENCY_LABEL: dict[str, dict[str, str]] = {
    "UAH": {"uk": "грн", "ru": "грн", "en": "UAH", "pl": "UAH"},
    "PLN": {"uk": "зл", "ru": "зл", "en": "PLN", "pl": "zł"},
    "EUR": {"uk": "євро", "ru": "евро", "en": "EUR", "pl": "EUR"},
    "CZK": {"uk": "крон", "ru": "крон", "en": "CZK", "pl": "CZK"},
}

_FROM = {"uk": "від", "ru": "от", "en": "from", "pl": "od"}
_TO = {"uk": "до", "ru": "до", "en": "to", "pl": "do"}
_ON_REQUEST = {
    "uk": "за консультацією",
    "ru": "по консультации",
    "en": "on request",
    "pl": "do ustalenia",
}


def lang_or_default(lang: str | None, default: str = "uk") -> str:
    return lang if lang in LANGS else default


def weekday_name(day: date, lang: str) -> str:
    return WEEKDAYS[lang_or_default(lang)][day.weekday()]


def short_date(day: date) -> str:
    return day.strftime("%d.%m")


def hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def day_label(day: date, lang: str) -> str:
    """'четвер, 22.10' — every date shown to a client carries its weekday."""
    return f"{weekday_name(day, lang)}, {short_date(day)}"


def currency_label(currency: str, lang: str) -> str:
    labels = _CURRENCY_LABEL.get(currency)
    if labels is None:
        return currency
    return labels[lang_or_default(lang)]


def money(amount: int, currency: str, lang: str) -> str:
    return f"{amount} {currency_label(currency, lang)}"


def price_text(
    price_type: str, price_min: int | None, price_max: int | None, currency: str, lang: str
) -> str:
    lang = lang_or_default(lang)
    if price_type == "on_request" or price_min is None:
        return _ON_REQUEST[lang]
    if price_type == "fixed":
        return money(price_min, currency, lang)
    if price_type == "from" or price_max is None:
        return f"{_FROM[lang]} {money(price_min, currency, lang)}"
    return f"{_FROM[lang]} {price_min} {_TO[lang]} {money(price_max, currency, lang)}"
