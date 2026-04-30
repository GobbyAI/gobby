"""Red tests for lifecycle/status validation gate mapping."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_gate_set_per_lifecycle_status_tuple() -> None:
    from gobby.tasks.validation import VALIDATION_GATES

    assert VALIDATION_GATES[("plan_review", "needs_review")] == "plan_review"
    assert VALIDATION_GATES[("test_arch", "needs_review")] == "test_arch"
    assert VALIDATION_GATES[("in_development", "needs_review")] == "qa"
    assert VALIDATION_GATES[("holistic_review", "needs_review")] == "holistic_review"
    assert VALIDATION_GATES[("pr", "needs_review")] == "pr"
