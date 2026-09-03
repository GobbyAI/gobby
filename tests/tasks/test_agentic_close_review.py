"""Automated detached close-review contract tests."""

from __future__ import annotations

import json
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
    assert 'changes_summary="summary"' in prompt
    assert "submit_close_review" in prompt
    assert "end_agent_run" in prompt
    assert "review_run_id" not in prompt
    assert "retry close_task" not in prompt
    assert "oversized" not in prompt.lower()
    assert "prior_requirements=" not in prompt
    assert "validation_commands=" not in prompt


def test_launch_prompt_carries_gate10_validation_runs() -> None:
    """The taskless validator cannot read the transcript, so gate 10's record rides along."""
    validation_commands = {
        "latest_outcomes": {"test": "success"},
        "latest_runs": [
            {
                "category": "test",
                "command": "uv run pytest tests/tasks/test_validation.py -q",
                "completed_at": "2026-09-03T05:10:00+00:00",
                "outcome": "success",
                "exit_code": 0,
            }
        ],
    }

    prompt = build_agentic_review_prompt(
        review_id="review",
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        validation_commands=validation_commands,
    )

    facts = json.dumps(validation_commands, sort_keys=True, default=str)
    assert f"validation_commands={facts}. " in prompt
    assert "gate 10's authoritative transcript record" in prompt
    assert "satisfies that command without any committed log or receipt" in prompt
    assert prompt.index("validation_commands=") < prompt.index("Inspect the task")


def test_launch_prompt_renders_prior_requirements() -> None:
    prior_requirements = (
        "Criterion 1: Focused tests pass.\n"
        "Gap: The integration path was not exercised.\n"
        "Required evidence: Run the real close adapter and capture its receipt."
    )

    prompt = build_agentic_review_prompt(
        review_id="review",
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        prior_requirements=prior_requirements,
    )

    assert f"prior_requirements={json.dumps(prior_requirements)}" in prompt
    assert (
        "A previously rejected close named this required evidence; reject the same criterion "
        "again unless the submitted code and evidence satisfy it."
    ) in prompt

    prompt_without_prior = build_agentic_review_prompt(
        review_id="review",
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        review_fingerprint="close",
        evidence_fingerprint="evidence",
    )

    assert "prior_requirements=" not in prompt_without_prior


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
    assert body["version"] == "1.6"
    assert '"required_evidence": null|"complete evidence set"' in body["prompts"]["agent"]
    assert "complete evidence set the next close has to supply" in body["prompts"]["agent"]
    # Gate 10's run record is the authority on command runs; the validator must
    # never demand a committed receipt or its own reproduction instead.
    guidance = body["prompts"]["agent"]
    assert "validation_commands facts are gate 10's transcript-derived" in guidance
    assert "That record is authoritative:" in guidance
    assert "other file committed to the repository as proof of a command run" in guidance
    assert "never reject a criterion because your own\nsandbox cannot reproduce it" in guidance
    assert "receipt or artifact that must result" not in guidance


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
