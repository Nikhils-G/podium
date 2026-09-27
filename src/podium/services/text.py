"""Tiny copy helpers shared by services and templates."""


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    """'1 review', '3 reviews' — never 'review(s)'."""
    word = singular if n == 1 else (plural_form or singular + "s")
    return f"{n:,} {word}"
