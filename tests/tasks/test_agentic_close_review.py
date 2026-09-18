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
        criterion_count=2,
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        review_fingerprint="close",
        evidence_fingerprint="evidence",
    )

    assert "review_id=review" in prompt
    assert "task_id=task" in prompt
    assert 'changes_summary="summary"' in prompt
    assert 'closure_reason="completed"' in prompt
    assert "This is a no-work disposition review" not in prompt
    assert "submit_close_review" in prompt
    assert "end_agent_run" in prompt
    assert "review_run_id" not in prompt
    assert "retry close_task" not in prompt
    assert "oversized" not in prompt.lower()
    assert "prior_requirements=" not in prompt
    assert "validation_commands=" not in prompt


def test_prompt_names_the_normalized_criterion_count_and_index_list() -> None:
    prompt = build_agentic_review_prompt(
        review_id="review",
        criterion_count=4,
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        review_fingerprint="close",
        evidence_fingerprint="evidence",
    )

    assert "criterion_count=4" in prompt
    assert "criterion_indexes=[1, 2, 3, 4]" in prompt
    assert "nested bullet" in prompt
    assert "exactly once" in prompt


@pytest.mark.parametrize(
    "reason", ["duplicate", "already_implemented", "wont_fix", "obsolete", "out_of_repo"]
)
def test_no_work_review_judges_disposition_instead_of_implementation(reason: str) -> None:
    prompt = build_agentic_review_prompt(
        review_id="review",
        criterion_count=2,
        task_id="task",
        commit_shas=[],
        changes_summary="The user superseded the wiki with complete retirement in epic #21771.",
        closure_reason=reason,
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        coordinator_owned_pending=True,
        prior_requirements="Implement the retired wiki renderer and tests.",
    )

    assert f"closure_reason={json.dumps(reason)}" in prompt
    assert "This is a no-work disposition review" in prompt
    assert "Reject only a missing, vague, or contradicted justification" in prompt
    assert "Deterministic gates still own attributed edits" in prompt
    assert "Apply prior requirements to the disposition justification only" in prompt
    assert "state `pending_external`" not in prompt
    assert "again unless the submitted code and evidence satisfy it" not in prompt


def test_agent_close_prompt_marks_live_criteria_pending_external() -> None:
    prompt = build_agentic_review_prompt(
        review_id="review",
        criterion_count=2,
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        coordinator_owned_pending=True,
    )

    assert "close caller is a spawned agent" in prompt
    assert "criterion beginning `Live:`" in prompt
    assert "state `pending_external`" in prompt
    assert "neither satisfied nor a gap" in prompt


def test_non_spawned_caller_prompt_forbids_pending_external() -> None:
    prompt = build_agentic_review_prompt(
        review_id="review",
        criterion_count=2,
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        review_fingerprint="close",
        evidence_fingerprint="evidence",
    )

    assert "close caller is not a spawned agent" in prompt
    assert "satisfied or gap" in prompt
    assert "`pending_external` is rejected as malformed" in prompt
    assert "state `pending_external`" not in prompt

    no_work = build_agentic_review_prompt(
        review_id="review",
        criterion_count=2,
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        closure_reason="duplicate",
    )
    assert "spawned agent" not in no_work


def test_launch_prompt_carries_gate10_validation_runs() -> None:
    """The taskless validator cannot read the transcript, so gate 10's record rides along."""
    validation_commands = {
        "latest_outcomes": {"test": "success"},
        "latest_runs": [
            {
                "category": "test",
                "command": "cd /repo && uv run pytest tests/tasks/test_validation.py -q",
                "core_command": "uv run pytest tests/tasks/test_validation.py -q",
                "wrapped": False,
                "completed_at": "2026-09-03T05:10:00+00:00",
                "outcome": "success",
                "exit_code": 0,
            }
        ],
        "uncredited_runs": [
            {
                "command": "uv run pytest tests/tasks/test_validation.py -q | tail -1",
                "reason": "wrapped",
                "wrapper_reason": "pipeline",
            }
        ],
        "criterion_commands": [
            {
                "command": "GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q",
                "core_command": "uv run pytest tests/tasks/test_validation.py -q",
                "status": "satisfied",
                "satisfied": True,
            }
        ],
    }

    prompt = build_agentic_review_prompt(
        review_id="review",
        criterion_count=2,
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
    assert "same normalization contract" in prompt
    assert "criterion_commands" in prompt
    assert "report every command gap in one verdict" in prompt
    assert "cite that entry when a verdict names a seen-but-uncredited run" in prompt
    assert prompt.index("validation_commands=") < prompt.index("Inspect the task")


def test_launch_prompt_renders_prior_requirements() -> None:
    prior_requirements = (
        "Criterion 1: Focused tests pass.\n"
        "Gap: The integration path was not exercised.\n"
        "Required evidence: Run the real close adapter and capture its receipt."
    )

    prompt = build_agentic_review_prompt(
        review_id="review",
        criterion_count=2,
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
        criterion_count=2,
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
    assert body["version"] == "1.10"
    assert "First apply the stated closure_reason" in body["prompts"]["agent"]
    assert '"state": "satisfied|gap|pending_external"' in body["prompts"]["agent"]
    assert "criterion beginning `Live:` case-insensitively" in body["prompts"]["agent"]
    assert "terminal closed, invalid, external_pending" in body["prompts"]["agent"]
    assert '"required_evidence": null|"complete evidence set"' in body["prompts"]["agent"]
    assert "complete evidence set the next close has to supply" in body["prompts"]["agent"]
    # Gate 10's run record is the authority on command runs; the validator must
    # never demand a committed receipt or its own reproduction instead.
    guidance = body["prompts"]["agent"]
    assert "validation_commands facts are gate 10's transcript-derived" in guidance
    assert "same normalization contract" in guidance
    assert "criterion_commands" in guidance
    assert "Report every command gap in one verdict" in guidance
    assert "uncredited_runs names commands" in guidance
    assert "transcript saw" in guidance
    assert "could not credit" in guidance
    assert "other file committed to the repository as proof of a command" in guidance
    assert "run, never ask for a run to be repeated" in guidance
    assert "never reject a criterion because" in guidance
    assert "your own sandbox cannot reproduce it" in guidance
    assert "receipt or artifact that must result" not in guidance


@pytest.mark.parametrize(
    ("status", "closed", "validation_status"),
    [
        ("closed", True, "valid"),
        ("invalid", False, "invalid"),
        ("external_pending", False, "pending"),
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


@pytest.mark.parametrize(
    ("blocking_reasons", "required_actions"),
    [
        pytest.param([], ["Inspect the invalid verdict."], id="zero"),
        pytest.param(
            ["Criterion 1 is unmet."],
            ["Implement criterion 1."],
            id="one",
        ),
        pytest.param(
            ["Criterion 1 is unmet.", "Criterion 2 is unmet."],
            ["Implement criterion 1.", "Implement criterion 2."],
            id="multiple",
        ),
    ],
)
def test_invalid_terminal_payload_reports_complete_remediation(
    blocking_reasons: list[str],
    required_actions: list[str],
) -> None:
    payload = build_terminal_review_payload(
        _review(status="running"),
        status="invalid",
        close_result={
            "blocking_reasons": blocking_reasons,
            "required_actions": required_actions,
        },
    )

    assert payload["outstanding_finding_count"] == len(blocking_reasons)
    assert payload["remediation_guidance"] == (
        "Address every listed blocking reason, validate the complete fix set, and commit the "
        "complete fix set before one resubmission."
    )
    assert payload["blocking_reasons"] == blocking_reasons
    assert payload["required_actions"] == required_actions


def test_external_pending_payload_names_coordinator_owned_criteria() -> None:
    payload = build_terminal_review_payload(
        _review(status="running"),
        status="external_pending",
        close_result={"pending_external_criteria": ["Live: restart the daemon."]},
    )

    assert payload["status"] == "external_pending"
    assert payload["closed"] is False
    assert payload["validation_status"] == "pending"
    assert payload["pending_external_criteria"] == ["Live: restart the daemon."]
    assert payload["required_actions"] == [
        "End this agent run; the coordinator must verify the pending Live criteria and close the task."
    ]


def test_terminal_payload_references_blockers_without_recopying_diagnostics() -> None:
    """The wake contract references blockers; it never re-carries bulk evidence."""
    blocker = "validation_commands: Run `uv run pytest tests/close.py -q` clean."
    payload = build_terminal_review_payload(
        _review(status="running"),
        status="invalid",
        close_result={
            "verdict": {"criteria": [{"index": 1, "satisfied": False}]},
            "checklist": [{"item": 10, "details": {"criterion_commands": ["x" * 50_000]}}],
            "transcript_evidence": {"validation_run_count": 640},
            "validation_commands": {"latest_runs": ["y" * 50_000]},
            "stable_facts": {"commit_shas": ["abc"]},
            "commit_shas": ["abc"],
            "message": blocker,
            "blocking_reasons": [blocker],
        },
        message=blocker,
    )

    assert payload["blocking_reasons"] == [blocker]
    assert payload["commit_shas"] == ["abc"]
    assert payload["outstanding_finding_count"] == 1
    # The blocker already states its action, so the generic remediation stands in.
    assert payload["required_actions"] == [
        "Address every blocking reason, rerun focused validation, commit fixes, and call "
        "close_task again."
    ]
    for key in (
        "verdict",
        "checklist",
        "transcript_evidence",
        "validation_commands",
        "stable_facts",
    ):
        assert key not in payload
    assert len(json.dumps(payload)) < 4_000


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
