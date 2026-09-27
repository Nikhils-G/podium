"""Colour tokens stay at WCAG AA: every text token against every surface, in both themes, is
checked straight from app.css, so a later tweak can't quietly bring back unreadable text."""

import re
from pathlib import Path

import pytest

CSS = (
    Path(__file__).resolve().parent.parent / "src" / "podium" / "static" / "css" / "app.css"
).read_text()
SURFACES = ("--page", "--surface", "--surface-raised", "--surface-sunken")
TEXT = ("--ink", "--ink-2", "--ink-muted", "--accent", "--good-text", "--critical-text")


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


def _themes() -> dict[str, dict[str, str]]:
    light = _tokens(CSS.split(":root {", 1)[1].split("}", 1)[0])
    dark = {**light, **_tokens(CSS.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0])}
    return {"light": light, "dark": dark}


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _ratio(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_tokens_meet_aa_on_every_surface(theme):
    tokens = _themes()[theme]
    failures = [
        f"{text} on {surface}: {_ratio(tokens[text], tokens[surface]):.2f}"
        for text in TEXT
        for surface in SURFACES
        if _ratio(tokens[text], tokens[surface]) < 4.5
    ]
    assert not failures, failures


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_white_text_on_the_accent_fill_meets_aa(theme):
    tokens = _themes()[theme]
    assert _ratio("#ffffff", tokens["--accent-fill"]) >= 4.5
    assert _ratio("#ffffff", tokens["--accent-fill-hover"]) >= 4.5


def test_primary_buttons_and_chosen_scores_use_the_fill():
    assert ".btn--primary { background: var(--accent-fill);" in CSS
    assert ".segmented__option input:checked + span { background: var(--accent-fill);" in CSS
