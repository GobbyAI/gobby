"""Automated oversized close-review contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import yaml

from gobby.storage.task_close_reviews import (
    TaskCloseReview,
    TaskCloseReviewStatus,
    TerminalTaskCloseReviewStatus,
)
from gobby.tasks.agentic_close_review import (
    TASK_CLOSE_VALIDATOR_AGENT,
    build_agentic_review_prompt,
    build_terminal_review_payload,
)

pytestmark = pytest.mark.unit


def test_agentic_review_prompt_is_taskless_and_submission_driven() -> None:
    prompt = build_agentic_review_prompt(
        review_id="review",
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        review_fingerprint="close",
        evidence_fingerprint="evidence",
    )

    assert "review_id=review" in prompt
    assert "task_id=task" in prompt
    assert "submit_close_review" in prompt
    assert "end_agent_run" in prompt
    assert "review_run_id" not in prompt
    assert "retry close_task" not in prompt


def test_task_close_validator_definition_submits_then_terminates() -> None:
    path = (
        Path(__file__).parents[2]
        / "src/gobby/install/shared/workflows/agents/task-close-validator.yaml"
    )
    body = yaml.safe_load(path.read_text())
    assert body["name"] == TASK_CLOSE_VALIDATOR_AGENT
    assert body["isolation"] == "none"
    blocked = set(body["blocked_mcp_tools"])
    assert {
        "gobby-tasks:close_task",
        "gobby-tasks:update_task",
        "gobby-agents:spawn_agent",
        "gobby-agents:stop_agent",
        "gobby-agents:kill_agent",
    } <= blocked
    step = body["step_workflow"]["steps"][0]
    assert "gobby-tasks:submit_close_review" in step["allowed_mcp_tools"]
    assert "gobby-agents:end_agent_run" in step["allowed_mcp_tools"]
    assert "gobby-agents:send_message" not in step["allowed_mcp_tools"]
    assert "submit_close_review" in body["prompts"]["agent"]


@pytest.mark.parametrize(
    ("status", "closed", "validation_status"),
    [
        ("closed", True, "valid"),
        ("invalid", False, "invalid"),
        ("stale", False, "error"),
        ("error", False, "error"),
    ],
)
def test_terminal_payload_has_stable_public_contract(
    status: str,
    closed: bool,
    validation_status: str,
) -> None:
    review = _review(status="running")

    payload = build_terminal_review_payload(
        review,
        status=cast(TerminalTaskCloseReviewStatus, status),
    )

    assert payload["event"] == "task_close_review_completed"
    assert payload["review_id"] == review.id
    assert payload["run_id"] == review.agent_run_id
    assert payload["task_id"] == review.task_id
    assert payload["task_ref"] == review.task_ref
    assert payload["status"] == status
    assert payload["closed"] is closed
    assert payload["validation_status"] == validation_status
    assert isinstance(payload["blocking_reasons"], list)
    assert isinstance(payload["required_actions"], list)


def _review(*, status: str) -> TaskCloseReview:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    return TaskCloseReview(
        id="review",
        task_id="task",
        task_ref="#42",
        caller_session_id="parent",
        agent_run_id="run",
        close_arguments={"preview": True},
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        status=cast(TaskCloseReviewStatus, status),
        result_payload=None,
        error=None,
        launched_at=now,
        completed_at=None,
        delivered_at=None,
        created_at=now,
        updated_at=now,
    )
