"""Cheap language detection for code templates (uk, ru, pl, en). The model picks its own
reply language from the prompt; this decides the language of disclosures and confirmations.

Ukrainian and Russian share most words, so Cyrillic text is scored by language-specific letters
and words. When the signals are missing or tied the answer is None, and the caller keeps the
previous language or the studio default (Ukrainian for Ukrainian studios).
"""

import re

_UK_LETTERS = set("іїєґІЇЄҐ")
_RU_LETTERS = set("ыэёъЫЭЁЪ")
_PL_LETTERS = set("ąćęłńśźżĄĆĘŁŃŚŹŻ")
_UK_WORDS = re.compile(
    r"\b(можна|дякую|будь|ласка|коли|записатися|записати|запишіть|добрий|що|чи|як|або|"
    r"також|прошу|потрібно|треба|ваш|завтра\s+на)\b"
)
_RU_WORDS = re.compile(
    r"\b(сколько|стоит|можно|спасибо|здравствуйте|пожалуйста|запишите|записаться|когда|"
    r"сегодня|ногти|маникюр|окно|свободно|привет|как|что|или|есть|нужно|надо|добрый)\b"
)
_PL_WORDS = re.compile(
    r"\b(dzień|dobry|cześć|ile|kosztuje|czy|jest|termin|wizyta|proszę|dziękuję|chciałabym|"
    r"chciałbym|jutro|paznokcie)\b"
)


def detect_language(text: str | None) -> str | None:
    """Return 'uk', 'ru', 'pl', 'en' or None when the text does not tell."""
    if not text:
        return None
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return None
    lowered = text.lower()
    if any(ch in _PL_LETTERS for ch in text) or _PL_WORDS.search(lowered):
        return "pl"
    cyrillic = sum("Ѐ" <= ch <= "ӿ" for ch in letters)
    if cyrillic / len(letters) > 0.5:
        uk = sum(ch in _UK_LETTERS for ch in text) * 2 + len(_UK_WORDS.findall(lowered))
        ru = sum(ch in _RU_LETTERS for ch in text) * 2 + len(_RU_WORDS.findall(lowered))
        if uk > ru:
            return "uk"
        if ru > uk:
            return "ru"
        return None
    if sum(ch.isascii() for ch in letters) / len(letters) > 0.8 and len(letters) >= 2:
        return "en"
    return None


def choose_language(
    detected: str | None, previous: str | None, allowed: list[str], default: str
) -> str:
    """Client's language if the studio allows it, otherwise the studio default. (Ukrainian law
    on the state language, art. 30 part 3: another language at the client's request.)"""
    language = detected or previous or default
    return language if language in allowed else default
