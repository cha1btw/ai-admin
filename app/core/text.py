"""Text helpers: splitting long replies for channel limits and cleaning model output."""

import re

_SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+|\n+")
_MARKDOWN = re.compile(r"(\*\*|__|`+|^#{1,6}\s*)", re.MULTILINE)


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _fits(text: str, max_bytes: int | None, max_chars: int | None) -> bool:
    if max_bytes is not None and utf8_len(text) > max_bytes:
        return False
    return max_chars is None or len(text) <= max_chars


def split_message(
    text: str, *, max_bytes: int | None = None, max_chars: int | None = None
) -> list[str]:
    """Split text into parts that fit the limits, preferring sentence, then word boundaries.

    Instagram limits text to 1000 UTF-8 bytes (Cyrillic letters take 2 bytes each), Telegram to
    4096 characters. A part never cuts a character in half.
    """
    text = text.strip()
    if not text:
        return []
    if _fits(text, max_bytes, max_chars):
        return [text]

    parts: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            parts.append(current)
            current = ""

    def add(piece: str, sep: str) -> bool:
        nonlocal current
        candidate = f"{current}{sep}{piece}" if current else piece
        if _fits(candidate, max_bytes, max_chars):
            current = candidate
            return True
        return False

    for unit in (u.strip() for u in _SENTENCE_BREAK.split(text)):
        if not unit or add(unit, " "):
            continue
        flush()
        if add(unit, " "):
            continue
        for word in unit.split(" "):
            if add(word, " "):
                continue
            flush()
            while not _fits(word, max_bytes, max_chars):
                cut = len(word)
                while cut > 1 and not _fits(word[:cut], max_bytes, max_chars):
                    cut -= 1
                parts.append(word[:cut])
                word = word[cut:]
            current = word
    flush()
    return parts


def clean_reply(text: str) -> str:
    """Remove markdown that chat apps would show literally and tidy whitespace."""
    text = _MARKDOWN.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
