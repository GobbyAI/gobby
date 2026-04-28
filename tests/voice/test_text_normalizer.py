"""Tests for TTS text normalization."""

from __future__ import annotations

import pytest

from gobby.voice.text_normalizer import normalize_tts_text

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("### *'Ship it'* for issue #123.", "Ship it for issue number 123."),
        ("#123 is ready.", "number 123 is ready."),
        (
            "Don't strip contractions or Gobby's possessives.",
            "Don't strip contractions or Gobby's possessives.",
        ),
        (
            "\u201cQuoted\u201d text and \u2018single quoted\u2019 text.",
            "Quoted text and single quoted text.",
        ),
        (
            "Use [the docs](https://example.com) & report 50%.",
            "Use the docs and report 50 percent.",
        ),
        ("- [x] `pytest` passed.", "pytest passed."),
        ("", ""),
        ("   \n\t", ""),
    ],
)
def test_normalize_tts_text_removes_markdown_symbols(raw: str, expected: str) -> None:
    assert normalize_tts_text(raw) == expected
