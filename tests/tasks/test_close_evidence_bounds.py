"""Bounded close evidence preserves exact commands without exporting whole sessions."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)

pytestmark = pytest.mark.unit


def _run(
    command: str,
    order: int,
    *,
    outcome: Literal["success", "failure", "unknown"] = "success",
    categories: tuple[str, ...] = (),
) -> TranscriptValidationRun:
    timestamp = datetime(2026, 9, 5, tzinfo=UTC) + timedelta(seconds=order)
    return TranscriptValidationRun(
        session_id="linked-session",
        source="codex",
        command=command,
        categories=categories,
        matcher_id="test",
        label="command",
        outcome=outcome,
        started_at=timestamp,
        completed_at=timestamp,
        order=order,
        exit_code=0 if outcome == "success" else 1 if outcome == "failure" else None,
    )


@pytest.mark.parametrize("outcome", ["success", "failure", "unknown"])
def test_manual_review_bounds_unrelated_commands_and_keeps_required_outcomes(
    outcome: Literal["success", "failure", "unknown"],
) -> None:
    evidence = TranscriptEvidence(
        command_runs=(
            _run("npm ci", 1),
            _run("npm ci", 2, outcome=outcome),
            *(
                _run(
                    f"python -c 'the web command and unrelated_{index} " + "x" * 6_000 + "'",
                    index + 10,
                )
                for index in range(1_512)
            ),
        ),
        validation_runs=(
            _run("npx prettier --check web", 3, categories=("format",)),
            *(
                _run(f"pytest unrelated-{index}.py", index + 10, categories=("test",))
                for index in range(236)
            ),
        ),
        sessions=("linked-session",),
    )

    gate = evaluate_validation_commands(
        task_category="manual",
        evidence=evidence,
        has_attributed_edits=False,
        validation_criteria="Run `npm ci` successfully; Prettier must pass for web checks.",
    )

    assert gate.status == ("failed" if outcome == "failure" else "skipped")
    assert len(json.dumps(dict(gate.details))) < 65_536
    latest = {run["core_command"]: run for run in gate.details["latest_runs"]}
    assert latest["npx prettier --check web"]["outcome"] == "success"
    # Unknown outcomes never replace a definitive run or become success evidence.
    assert latest["npm ci"]["outcome"] == ("success" if outcome == "unknown" else outcome)
    assert latest["npm ci"]["command"] == "npm ci"
    assert gate.details["omitted_latest_run_count"] > 1_500
    if outcome == "unknown":
        assert {"command": "npm ci", "reason": "unknown outcome"} in gate.details["uncredited_runs"]


@pytest.mark.parametrize("required", ["npm ci", "CI=1 npm ci", "npx prettier --check web"])
def test_exact_core_command_survives_saturated_incidental_matches(required: str) -> None:
    evidence = TranscriptEvidence(
        command_runs=(
            _run(required, 1),
            *(_run(f"python -c 'the command and web_{index}'", index + 2) for index in range(100)),
        ),
    )
    gate = evaluate_validation_commands(
        task_category="manual",
        evidence=evidence,
        has_attributed_edits=False,
        validation_criteria="Run `npm ci`; Prettier and the command and web checks must succeed.",
    )
    core = "npm ci" if required.endswith("npm ci") else required
    required_run = next(run for run in gate.details["latest_runs"] if run["core_command"] == core)
    assert required_run["command"] == required
    assert required_run["outcome"] == "success"


def test_bounded_review_keeps_code_gate_failure_outside_selected_sample() -> None:
    evidence = TranscriptEvidence(
        validation_runs=(_run("pytest important.py", 1, outcome="failure", categories=("test",)),),
        command_runs=tuple(_run(f"inspect-unrelated-{index}", index + 2) for index in range(100)),
    )
    gate = evaluate_validation_commands(
        task_category="code", evidence=evidence, has_attributed_edits=True
    )
    assert gate.status == "failed"
    assert gate.details["latest_outcomes"] == {"test": "failure"}
    assert gate.details["unresolved_failure_categories"] == ["test"]


def test_referenced_uncredited_commands_precede_large_diagnostic_sample() -> None:
    edit = TranscriptEdit(
        session_id="linked-session",
        source="codex",
        path="web/package.json",
        timestamp=datetime(2026, 9, 5, tzinfo=UTC),
        order=2,
        tool_name="apply_patch",
    )
    evidence = TranscriptEvidence(
        command_runs=(
            _run("npm ci", 1),
            _run("npm ci; echo done", 3),
            _run("npx prettier --check web", 4, outcome="unknown"),
            *(_run(f"inspect-{index} " + "x" * 1_000, index + 5) for index in range(100)),
        ),
        edits=(edit,),
    )
    gate = evaluate_validation_commands(
        task_category="manual",
        evidence=evidence,
        has_attributed_edits=True,
        validation_criteria="Run npm ci and Prettier successfully.",
    )
    assert gate.status == "skipped"
    assert len(json.dumps(dict(gate.details))) < 65_536
    stale = next(
        run
        for run in gate.details["uncredited_runs"]
        if run["reason"] == "stale after a later task edit"
    )
    assert stale["core_command"] == "npm ci"
    assert stale["order"] == 1
    assert stale["invalidating_edit"]["path"] == "web/package.json"
    assert {"command": "npx prettier --check web", "reason": "unknown outcome"} in gate.details[
        "uncredited_runs"
    ]
    wrapped = next(
        run for run in gate.details["latest_runs"] if run["command"] == "npm ci; echo done"
    )
    assert wrapped["core_command"] is None
    assert wrapped["wrapped"] is True
    assert all(run["core_command"] != "npm ci" for run in gate.details["latest_runs"])


def test_excluded_runs_and_nearest_observed_run_are_byte_bounded() -> None:
    commands = [f"python -c 'unrelated_{index} " + "x" * 6_000 + "'" for index in range(20)]
    edit = TranscriptEdit(
        session_id="linked-session",
        source="codex",
        path="src/gobby/tasks/close_checklist.py",
        timestamp=datetime(2026, 9, 5, tzinfo=UTC) + timedelta(seconds=50),
        order=50,
        tool_name="apply_patch",
    )
    gate = evaluate_validation_commands(
        task_category="manual",
        evidence=TranscriptEvidence(
            command_runs=tuple(_run(command, index + 1) for index, command in enumerate(commands)),
            edits=(edit,),
            sessions=("linked-session",),
        ),
        has_attributed_edits=True,
    )
    excluded = gate.details["excluded_runs"]
    nearest = gate.details["nearest_observed_run"]
    assert len(excluded) == 16
    assert nearest is not None
    assert len(json.dumps(excluded)) < 32_768
    assert len(json.dumps(nearest)) < 4_096
    newest = commands[-1]
    digest = hashlib.sha256(newest.encode()).hexdigest()
    assert nearest["command"].startswith(newest[:256])
    assert "command excerpt" in nearest["command"]
    assert digest in nearest["command"]
    assert nearest["reason"]
    assert nearest["remedy"]
    assert nearest["invalidating_edit"]["path"] == "src/gobby/tasks/close_checklist.py"
    assert nearest.get("core_command") in {None, nearest["command"]}
    assert nearest in excluded
    for record in excluded:
        assert "command excerpt" in record["command"]
        assert "sha256=" in record["command"]
        assert record["reason"]
        assert record["remedy"]
        assert record["invalidating_edit"]["path"] == "src/gobby/tasks/close_checklist.py"


def test_oversized_script_is_omitted_without_exact_command_credit() -> None:
    command = "inspect-unrelated " + "x" * 20_000
    gate = evaluate_validation_commands(
        task_category="manual",
        evidence=TranscriptEvidence(command_runs=(_run(command, 1),)),
        has_attributed_edits=False,
    )
    assert gate.details["latest_runs"] == []
    assert gate.details["omitted_latest_run_count"] == 1
    assert "not proof of failure or absence" in gate.details["evidence_selection"]


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("detail", ["concise", "diagnostic"])
def test_close_response_omits_bulk_review_evidence(closed: bool, detail: str) -> None:
    evaluation = CloseEvaluation("#42", response_detail=detail)
    evaluation.extra = {
        "validation_commands": {"latest_runs": ["x" * 100_000]},
        "stable_facts": {"attributed_paths": ["a.py"]},
        "criteria_review_duration_ms": 3,
    }
    response = evaluation.response(preview=not closed, closed=closed)
    # validation_commands and stable_facts are internal carriers for the validator
    # launch and the persisted review; the checklist owns them in responses.
    assert "validation_commands" not in response
    assert "stable_facts" not in response
    assert response["criteria_review_duration_ms"] == 3
    assert len(json.dumps(response)) < 1_000


def _stale_criterion_evidence(
    command_count: int,
    forms_per_command: int,
) -> tuple[TranscriptEvidence, str]:
    """Many criterion commands, each with many stale observed forms."""
    criteria = " ".join(
        f"`uv run pytest tests/close/bounds_{index}.py -q` passes."
        for index in range(command_count)
    )
    runs: list[TranscriptValidationRun] = []
    order = 0
    for index in range(command_count):
        for repeat in range(forms_per_command):
            order += 1
            runs.append(
                _run(
                    f"GOBBY_TEST_PROTECT={repeat} uv run pytest tests/close/bounds_{index}.py -q",
                    order,
                )
            )
    edit = TranscriptEdit(
        session_id="linked-session",
        source="codex",
        path="src/gobby/tasks/close_checklist.py",
        timestamp=datetime(2026, 9, 5, tzinfo=UTC) + timedelta(seconds=order + 1),
        order=order + 1,
        tool_name="apply_patch",
    )
    return (
        TranscriptEvidence(
            validation_runs=tuple(runs), edits=(edit,), sessions=("linked-session",)
        ),
        criteria,
    )


def test_criterion_command_sections_share_the_review_budget() -> None:
    evidence, criteria = _stale_criterion_evidence(command_count=16, forms_per_command=40)
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
        validation_criteria=criteria,
    )
    assert gate.status == "failed"
    details = gate.details
    gaps = details["criterion_command_gaps"]
    # Gap entries are compact references; full records live in criterion_commands.
    assert len(gaps) == 16
    assert all(set(gap) == {"command", "core_command", "status"} for gap in gaps)
    records = details["criterion_commands"]
    assert records
    assert all(len(record.get("observed_forms", ())) <= 16 for record in records)
    assert any(record.get("omitted_observed_form_count") for record in records)
    assert details["omitted_criterion_command_count"] >= 1
    assert len(gate.message) < 2_500
    assert "omitted" in gate.message
    assert len(json.dumps(details)) < 60_000


def test_each_failed_gate_contributes_one_prefixed_blocker_sentence() -> None:
    evaluation = CloseEvaluation("#7", response_detail="concise")
    evaluation.collect_failure(
        9,
        "uncommitted_task_edits",
        "uncommitted_task_edits",
        "Task-attributed files are dirty. Commit them and retry.",
    )
    evaluation.fail(
        10,
        "validation_commands",
        "validation_command_required",
        "Run `uv run pytest tests/close_evidence_bounds.py -q` clean after the final edit.",
        action="Run the focused bounds test clean after the final edit.",
    )
    response = evaluation.response(preview=True)
    assert response["message"] == "Task-attributed files are dirty. Commit them and retry."
    # Only the explicit action is carried; no field repeats another field's text.
    assert response["action"] == "Run the focused bounds test clean after the final edit."
    assert response["blocking_reasons"] == [
        "uncommitted_task_edits: Task-attributed files are dirty. Commit them and retry.",
        "validation_commands: Run `uv run pytest tests/close_evidence_bounds.py -q` clean "
        "after the final edit.",
    ]
    assert response["required_actions"] == [
        "Run the focused bounds test clean after the final edit."
    ]
    assert response["message"] not in response["blocking_reasons"]
    assert not set(response["required_actions"]) & set(response["blocking_reasons"])


def test_diagnostic_close_payload_carries_each_section_once_within_bound() -> None:
    """The 65,536-character close-payload contract survives many evidence sections."""
    from dataclasses import replace as dataclass_replace

    from gobby.tasks.agentic_close_review import build_agentic_review_prompt
    from gobby.tasks.close_checklist import CloseGateResult

    evidence, criteria = _stale_criterion_evidence(command_count=16, forms_per_command=40)
    command_gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
        validation_criteria=criteria,
    )
    scope_details = {
        "declared_scope": ["src/gobby/tasks/"],
        "actual_paths": [f"src/gobby/tasks/module_{index}.py" for index in range(40)],
        "out_of_scope_paths": [f"src/gobby/tasks/module_{index}.py" for index in range(5)],
        "advisory_scope": ["docs/"],
        "advisory_scope_drift": ["docs/notes.md"],
    }
    evaluation = CloseEvaluation("#22372", response_detail="diagnostic")
    evaluation.commit_shas = ["a" * 40, "b" * 40]
    evaluation.edited_paths = set(scope_details["actual_paths"])
    evaluation.collect_failure(
        8,
        "task_scope",
        "task_scope_mismatch",
        "A scope_justification is required for out-of-scope paths.",
        action=(
            "Pass a specific scope_justification between 20 and 1000 characters "
            "that explains why the listed paths belong in this task."
        ),
        details=scope_details,
        extra=scope_details,
    )
    evaluation.extra["validation_commands"] = command_gate.details
    evaluation.record_gate_failure(
        dataclass_replace(command_gate, item=10),
        error="validation_command_required",
    )
    evaluation.extra["stable_facts"] = {
        "commit_count": 2,
        "commit_shas": list(evaluation.commit_shas),
        "had_attributed_edits": True,
        "attributed_paths": sorted(evaluation.edited_paths),
    }
    response = evaluation.response(preview=True)
    serialized = json.dumps(response)
    assert len(serialized) < 65_536
    # The scope inventory, run record, and stable facts live in exactly one
    # section each: the checklist gate details, never also as top-level fields.
    assert not (
        {
            "declared_scope",
            "actual_paths",
            "out_of_scope_paths",
            "advisory_scope",
            "advisory_scope_drift",
            "validation_commands",
            "stable_facts",
        }
        & set(response)
    )
    checklist = {entry["name"]: entry for entry in response["checklist"]}
    assert checklist["task_scope"]["details"] == scope_details
    assert checklist["validation_commands"]["details"] == command_gate.details
    assert checklist["validation_commands"]["item"] == 10
    # One concise sentence per blocker, gate-prefixed in the blocker list.
    assert response["message"] == "A scope_justification is required for out-of-scope paths."
    assert [reason.split(": ", 1)[0] for reason in response["blocking_reasons"]] == [
        "task_scope",
        "validation_commands",
    ]
    assert response["message"] not in response["blocking_reasons"]
    assert response["required_actions"] == [
        "Pass a specific scope_justification between 20 and 1000 characters "
        "that explains why the listed paths belong in this task."
    ]
    # The agentic review launch payload serializes the same bounded record once.
    prompt = build_agentic_review_prompt(
        review_id="review",
        task_id="task",
        commit_shas=evaluation.commit_shas,
        changes_summary="Deduplicated the close payload sections.",
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        validation_commands=command_gate.details,
    )
    assert prompt.count("validation_commands=") == 1
    assert len(prompt) < 65_536
    assert "criterion_command_gaps entries are compact references" in prompt
