"""Lifecycle monitor stale-claim recovery must be stage-native."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def _recovery_source() -> str:
    return source_text("src/gobby/agents/task_recovery.py")


def test_recover_stale_claims_uses_projected_stage_state() -> None:
    source = _recovery_source()

    assert "projected_task_state" in source
    assert ".status" not in source


def test_recover_in_progress_failure_releases_claim_without_status_open() -> None:
    source = _recovery_source()

    assert "fail_stage" in source
    assert "release_task_claim" in source
    assert "dispatch_failure_count" in source
    assert "status='open'" not in source


def test_recover_non_development_states_clear_ownership_no_stage_transition() -> None:
    source = _recovery_source()

    assert 'lifecycle_stage != "in_progress"' in source
    assert "release_task_claim" in source
    assert "reject_review(" not in source


def test_unrecoverable_marks_canonical_escalation_fields_not_status_escalated() -> None:
    source = _recovery_source()

    assert "escalated_at" in source
    assert "escalation_reason" in source
    assert "status='escalated'" not in source
