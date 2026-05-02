"""Lifecycle monitor stale-claim recovery must be stage-native."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def _source() -> str:
    return source_text("src/gobby/agents/lifecycle_monitor.py")


def test_recover_stale_claims_uses_current_stage() -> None:
    assert "current_stage" in _source()
    assert "lifecycle_stage" not in _source()


def test_recover_in_progress_cancellation_calls_fail_stage_not_status_open() -> None:
    source = _source()

    assert "fail_stage(" in source
    assert "status='open'" not in source


def test_recover_needs_review_clears_ownership_no_stage_transition() -> None:
    source = _source()

    assert "needs_review" in source
    assert "release_task_claim(" in source


def test_recover_review_approved_clears_ownership_no_stage_transition() -> None:
    source = _source()

    assert "review_approved" in source
    assert "release_task_claim(" in source


def test_recover_in_progress_does_not_call_reject_review() -> None:
    assert "reject_review(" not in _source()


def test_recover_unrecoverable_calls_escalate_task_not_status_escalated() -> None:
    source = _source()

    assert "escalate_task(" in source
    assert "status='escalated'" not in source
