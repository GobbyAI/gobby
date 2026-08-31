"""Tests for the shared blocking-effect deadline."""

from unittest.mock import patch

import pytest

from gobby.hooks import effect_deadline


def test_new_blocking_effect_deadline_allows_load_jitter() -> None:
    with patch("gobby.hooks.effect_deadline.time.monotonic", return_value=100.0):
        assert effect_deadline.BLOCKING_EFFECT_BUDGET_SECONDS == 20.0
        deadline = effect_deadline.new_blocking_effect_deadline()
        assert isinstance(deadline, effect_deadline.BlockingEffectDeadline)
        assert effect_deadline.remaining_blocking_effect_seconds(deadline, maximum=30.0) == 20.0


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
            effect_deadline.remaining_blocking_effect_seconds(
                effect_deadline.BlockingEffectDeadline(deadline),
                maximum=maximum,
            )
            == expected
        )


@pytest.mark.parametrize(
    ("deadline", "expected"),
    [
        (None, False),
        (105.0, False),
        (100.0, True),
        (99.0, True),
    ],
)
def test_blocking_budget_overrun_reports_a_spent_budget(
    deadline: float | None,
    expected: bool,
) -> None:
    """Effects that reserve a floor run regardless, so this reports rather than gates."""
    with patch("gobby.hooks.effect_deadline.time.monotonic", return_value=100.0):
        assert (
            effect_deadline.blocking_budget_overrun(
                None if deadline is None else effect_deadline.BlockingEffectDeadline(deadline)
            )
            is expected
        )


def test_blocking_effect_deadline_extends_by_exact_queue_wait() -> None:
    deadline = effect_deadline.BlockingEffectDeadline(101.0)

    deadline.extend(4.25)

    with patch("gobby.hooks.effect_deadline.time.monotonic", return_value=100.0):
        assert effect_deadline.remaining_blocking_effect_seconds(deadline, maximum=10.0) == 5.25
