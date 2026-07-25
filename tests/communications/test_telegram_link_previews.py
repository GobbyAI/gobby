"""Tests for Telegram link preview option validation and precedence."""

from __future__ import annotations

import pytest

from gobby.communications.telegram_link_previews import (
    normalize_link_preview_options,
    resolve_link_preview_options,
)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("disabled", "must be an object or null"),
        ({"is_disabled": 1}, "is_disabled must be a boolean"),
        ({"url": False}, "url must be a string"),
        ({"future_option": True}, "unsupported fields: future_option"),
    ],
)
def test_normalize_link_preview_options_rejects_invalid_values(
    value: object,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        normalize_link_preview_options(value, field_name="preview")


def test_resolve_link_preview_options_copies_merges_and_clears_defaults() -> None:
    default = {"is_disabled": True, "show_above_text": True}

    inherited = resolve_link_preview_options(default, {})
    merged = resolve_link_preview_options(
        default,
        {"link_preview_options": {"is_disabled": False}},
    )
    cleared = resolve_link_preview_options(
        default,
        {"link_preview_options": None},
    )

    assert inherited == default
    assert inherited is not default
    assert merged == {"is_disabled": False, "show_above_text": True}
    assert cleared is None
