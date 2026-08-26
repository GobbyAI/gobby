"""Tests for the shared blocking-effect deadline."""

from unittest.mock import patch

import pytest

from gobby.hooks import effect_deadline


def test_new_blocking_effect_deadline_allows_load_jitter() -> None:
    with patch("gobby.hooks.effect_deadline.time.monotonic", return_value=100.0):
        assert effect_deadline.BLOCKING_EFFECT_BUDGET_SECONDS == 20.0
        assert effect_deadline.new_blocking_effect_deadline() == 120.0


@pytest.mark.parametrize(
    ("deadline", "maximum", "expected"),
    [
        (105.0, 2.0, 2.0),
        (101.0, 2.0, 1.0),
        (99.0, 2.0, 0.0),
    ],
)
def test_remaining_blocking_effect_seconds_preserves_per_effect_cap(
    deadline: float,
    maximum: float,
    expected: float,
) -> None:
    with patch("gobby.hooks.effect_deadline.time.monotonic", return_value=100.0):
        assert (
            effect_deadline.remaining_blocking_effect_seconds(deadline, maximum=maximum) == expected
        )
