"""Pipeline heartbeat stale-task recovery must be stage-native."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def _source() -> str:
    return source_text("src/gobby/workflows/pipeline_heartbeat.py")


def test_heartbeat_filters_on_current_stage_predicate_not_status() -> None:
    source = _source()

    assert "current_stage" in source
    assert "list_tasks(status=" not in source


def test_heartbeat_in_progress_with_commits_calls_submit_for_review_not_status_needs_review() -> (
    None
):
    source = _source()

    assert "submit_for_review(" in source
    assert "status='needs_review'" not in source


def test_heartbeat_in_progress_no_commits_recovers_abandoned_stage_not_status_open() -> None:
    source = _source()

    assert "recover_abandoned_stage(" in source
    assert "status='open'" not in source


def test_heartbeat_in_progress_no_commits_does_not_call_reject_review() -> None:
    assert "reject_review(" not in _source()


def test_heartbeat_submit_branch_only_used_from_in_progress_with_commits() -> None:
    source = _source()

    assert "in_progress" in source
    assert "commits" in source
    assert "submit_for_review(" in source
