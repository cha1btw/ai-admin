"""Phone normalization to E.164. Ukrainian local formats are the default."""

import re


def normalize_phone(raw: str | None) -> str | None:
    """'067 123 45 67', '+38 (067) 123-45-67', '380671234567' -> '+380671234567'.

    Returns None when the input cannot be a phone number. International numbers with a
    leading + (Poland, Czechia, Germany and others) are accepted as 8-15 digits.
    """
    if not raw:
        return None
    compact = re.sub(r"[\s\-().]", "", raw.strip())
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    has_plus = compact.startswith("+")
    digits = compact.lstrip("+")
    if not digits.isdigit():
        return None
    if has_plus:
        if digits.startswith("380"):
            return "+" + digits if len(digits) == 12 else None
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if len(digits) == 12 and digits.startswith("380"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("80"):
        return "+3" + digits
    if len(digits) == 10 and digits.startswith("0"):
        return "+38" + digits
    if len(digits) == 9 and digits[0] != "0":
        return "+380" + digits
    return None


def phone_digits(text: str) -> str:
    return re.sub(r"\D", "", text)
