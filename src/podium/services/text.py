"""Tiny copy helpers shared by services and templates."""

from datetime import datetime


def plural(n, singular: str, plural_form: str | None = None) -> str:
    """'1 review', '3 reviews' — never 'review(s)'. Accepts anything int() understands."""
    try:
        count = int(n)
    except (TypeError, ValueError):
        return f"{n} {plural_form or singular + 's'}"
    word = singular if count == 1 else (plural_form or singular + "s")
    return f"{count:,} {word}"


def utc_text(value: datetime | None) -> str:
    """'30 Sep 2026, 18:15 UTC' for sentences the server writes (confirmations, notes)."""
    return f"{value.day} {value:%b %Y, %H:%M} UTC" if value else ""
