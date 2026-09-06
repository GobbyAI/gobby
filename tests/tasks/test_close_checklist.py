from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest

from gobby.tasks.close_checklist import (
    CloseGateResult,
    evaluate_validation_commands,
    first_failed_gate,
)
from gobby.tasks.transcript_evidence import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
    TranscriptValidationSegment,
    merge_transcript_evidence,
)

BASE_TIME = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
EvidenceOutcome = Literal["success", "failure", "unknown"]


def _run(
    order: int,
    *,
    outcome: str = "success",
    categories: tuple[str, ...] = ("test",),
    command: str = "pytest",
) -> TranscriptValidationRun:
    exit_code = 0 if outcome == "success" else 1 if outcome == "failure" else None
    return TranscriptValidationRun(
        session_id="session-1",
        source="codex",
        command=command,
        categories=categories,
        matcher_id="test-matcher",
        label="Test command",
        outcome=cast(EvidenceOutcome, outcome),
        exit_code=exit_code,
        started_at=BASE_TIME + timedelta(seconds=order - 1),
        completed_at=BASE_TIME + timedelta(seconds=order),
        order=order,
    )


def _audit_run(
    order: int,
    *,
    outcome: str = "success",
    command: str = (
        "uv run gobby test-types audit tests/ "
        "--baseline .gobby/test-types-baseline.json --fail-on-new"
    ),
    normalized_command: str = (
        "gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new"
    ),
) -> TranscriptValidationRun:
    return replace(
        _run(order, outcome=outcome, categories=("type_check",), command=command),
        matcher_id="gobby-test-types-audit",
        label="Gobby test-types ratchet",
        validation_segments=(
            TranscriptValidationSegment(
                command=normalized_command,
                categories=("type_check",),
            ),
        ),
    )


def _edit(order: int) -> TranscriptEdit:
    return TranscriptEdit(
        session_id="session-1",
        source="codex",
        path="src/example.py",
        timestamp=BASE_TIME + timedelta(seconds=order),
        order=order,
        tool_name="apply_patch",
    )


@pytest.mark.parametrize("category", ["code", "refactor", "test"])
def test_code_categories_require_clean_test_run(category: str) -> None:
    gate = evaluate_validation_commands(
        task_category=category,
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"


@pytest.mark.parametrize("category", ["docs", "planning", "research", "manual"])
def test_non_command_categories_skip_validation(category: str) -> None:
    gate = evaluate_validation_commands(
        task_category=category,
        evidence=TranscriptEvidence(),
        has_attributed_edits=True,
    )

    assert gate.status == "skipped"
    assert gate.details["skip_reason"] == "category"


def test_no_edit_task_skips_validation_for_any_category() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(),
        has_attributed_edits=False,
    )

    assert gate.status == "skipped"
    assert gate.details["skip_reason"] == "no-edit"


def test_python_test_change_requires_whole_tree_test_types_audit() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _run(1, categories=("type_check",), command="mypy src/"),
                _run(2),
            )
        ),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "failed"
    assert gate.details["test_types_audit_required"] is True
    assert (
        "uv run gobby test-types audit tests/ "
        "--baseline .gobby/test-types-baseline.json --fail-on-new"
    ) in gate.message


def test_canonical_test_types_audit_and_test_run_pass() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_audit_run(1), _run(2))),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "passed"
    assert gate.details["latest_test_types_audit"]["outcome"] == "success"


def test_generic_type_check_cannot_cure_failed_test_types_audit() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _audit_run(1, outcome="failure"),
                _run(2, categories=("type_check",), command="mypy src/"),
                _run(3),
            )
        ),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "failed"
    assert "last failed" in gate.message
    assert gate.details["latest_outcomes"]["type_check"] == "success"


def test_later_canonical_test_types_audit_cures_failure() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_audit_run(1, outcome="failure"), _run(2), _audit_run(3))
        ),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "passed"
    assert gate.details["latest_test_types_audit"]["outcome"] == "success"


def test_stale_test_types_audit_does_not_satisfy_guard() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_audit_run(1), _run(3)),
            edits=(_edit(2),),
        ),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "failed"
    assert gate.details["latest_test_types_audit"] is None


@pytest.mark.parametrize(
    ("command", "normalized_command"),
    [
        (
            "uv run gobby test-types audit tests/tasks/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new",
            "gobby test-types audit tests/tasks/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new",
        ),
        (
            "uv run gobby test-types audit tests/ --baseline other.json --fail-on-new",
            "gobby test-types audit tests/ --baseline other.json --fail-on-new",
        ),
        (
            "uv run gobby test-types audit tests/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new --write-baseline",
            "gobby test-types audit tests/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new --write-baseline",
        ),
        (
            "uv run gobby test-types audit tests/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new "
            "--allow-failing-baseline",
            "gobby test-types audit tests/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new "
            "--allow-failing-baseline",
        ),
    ],
)
def test_noncanonical_test_types_audits_do_not_satisfy_guard(
    command: str,
    normalized_command: str,
) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _audit_run(1, command=command, normalized_command=normalized_command),
                _run(2),
            )
        ),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "failed"
    assert gate.details["latest_test_types_audit"] is None


@pytest.mark.parametrize(
    "command",
    [
        "GOBBY_TEST_PROTECT=1 uv run gobby test-types audit tests/ "
        "--baseline .gobby/test-types-baseline.json --fail-on-new",
        "poetry run gobby test-types audit tests/ "
        "--baseline .gobby/test-types-baseline.json --fail-on-new",
        "cd /repo && uv run gobby test-types audit tests/ "
        "--baseline .gobby/test-types-baseline.json --fail-on-new",
    ],
)
def test_supported_test_types_audit_prefixes_are_accepted(command: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_audit_run(1, command=command), _run(2))),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "passed"


def test_compound_test_types_audit_does_not_satisfy_guard() -> None:
    command = (
        "uv run gobby test-types audit tests/ "
        "--baseline .gobby/test-types-baseline.json --fail-on-new && "
        "uv run pytest tests/tasks/test_close_checklist.py"
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_audit_run(1, command=command), _run(2))),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "failed"
    assert gate.details["latest_test_types_audit"] is None


@pytest.mark.parametrize("changed_path", ["tests/deleted.py", "tests/renamed.py"])
def test_deleted_or_renamed_python_test_paths_trigger_guard(changed_path: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
        changed_paths=(changed_path,),
    )

    assert gate.status == "failed"


def test_non_test_changes_keep_existing_validation_behavior() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
        changed_paths=("src/gobby/tasks/close_checklist.py",),
    )

    assert gate.status == "passed"
    assert gate.details["test_types_audit_required"] is False


@pytest.mark.parametrize("category,has_edits", [("manual", True), ("code", False)])
@pytest.mark.parametrize("outcome", ["success", "failure", "unknown"])
def test_exempt_review_retains_command_evidence(
    category: str, has_edits: bool, outcome: str
) -> None:
    evidence = TranscriptEvidence(
        validation_runs=(_run(1, categories=("format",), command="npx prettier --check web"),),
        command_runs=(_run(2, outcome=outcome, categories=(), command="npm ci"),),
        sessions=("session-1",),
    )
    gate = evaluate_validation_commands(
        task_category=category, evidence=evidence, has_attributed_edits=has_edits
    )
    assert gate.status == "skipped"
    assert gate.details["sessions"] == ["session-1"]
    assert gate.details["latest_runs"][0]["command"] == "npx prettier --check web"
    if outcome == "unknown":
        assert len(gate.details["latest_runs"]) == 1
        assert {"command": "npm ci", "reason": "unknown outcome"} in gate.details["uncredited_runs"]
    else:
        run = gate.details["latest_runs"][1]
        assert (run["core_command"], run["category"], run["outcome"]) == ("npm ci", None, outcome)
    assert "test" not in gate.details["latest_outcomes"]


@pytest.mark.parametrize("category", ["code", "config"])
def test_uncategorized_success_never_satisfies_required_validation(category: str) -> None:
    gate = evaluate_validation_commands(
        task_category=category,
        evidence=TranscriptEvidence(command_runs=(_run(1, categories=(), command="npm ci"),)),
        has_attributed_edits=True,
    )
    assert gate.status == "failed"
    assert gate.details["latest_outcomes"] == {}


@pytest.mark.parametrize("rerun", [False, True])
def test_review_only_commands_keep_merge_freshness_and_wrapper_diagnostics(rerun: bool) -> None:
    owner = TranscriptEvidence(command_runs=(_run(1, categories=(), command="npm ci"),))
    later = TranscriptEvidence(
        command_runs=(
            _run(3, categories=(), command="npm ci; echo done"),
            _run(4, categories=(), command="unrecognized-check", outcome="unknown"),
        ),
        edits=(replace(_edit(2), session_id="session-2"),),
    )
    if rerun:
        later = replace(
            later, command_runs=(*later.command_runs, _run(5, categories=(), command="npm ci"))
        )
    evidence = merge_transcript_evidence(owner, later)
    gate = evaluate_validation_commands(
        task_category="manual", evidence=evidence, has_attributed_edits=True
    )
    assert gate.status == "skipped"
    assert {"command": "npm ci", "reason": "stale after a later task edit"} in gate.details[
        "uncredited_runs"
    ]
    assert {"command": "unrecognized-check", "reason": "unknown outcome"} in gate.details[
        "uncredited_runs"
    ]
    wrapped = next(run for run in gate.details["latest_runs"] if run["wrapped"])
    assert wrapped["command"] == "npm ci; echo done"
    assert wrapped["core_command"] is None
    assert any(item["reason"] == "wrapped" for item in gate.details["uncredited_runs"])
    credited = [run for run in gate.details["latest_runs"] if not run["wrapped"]]
    assert [run["core_command"] for run in credited] == (["npm ci"] if rerun else [])


def test_config_accepts_any_clean_validation_command() -> None:
    gate = evaluate_validation_commands(
        task_category="config",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1, categories=("lint",), command="ruff check"),)
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"


def test_latest_definitive_result_per_category_cures_failure() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _run(1, outcome="failure"),
                _run(2, outcome="success"),
            )
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"
    assert gate.details["latest_outcomes"] == {"test": "success"}
    # The winning run is recorded in full so the criteria reviewer can treat
    # it as the authoritative account of the command instead of a receipt.
    assert gate.details["latest_runs"] == [
        {
            "category": "test",
            "command": "pytest",
            "core_command": "pytest",
            "wrapped": False,
            "completed_at": (BASE_TIME + timedelta(seconds=2)).isoformat(),
            "outcome": "success",
            "exit_code": 0,
        }
    ]


def test_latest_runs_lists_every_distinct_command_in_shared_category() -> None:
    pytest_command = "uv run pytest tests/tasks/test_close_checklist.py -q"
    quality_command = (
        "uv run gobby test-quality audit tests/tasks/test_close_checklist.py "
        "--baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low"
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _run(1, command=pytest_command),
                _run(2, command=quality_command),
            )
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"
    assert gate.details["latest_outcomes"] == {"test": "success"}
    assert gate.details["latest_runs"] == [
        {
            "category": "test",
            "command": pytest_command,
            "core_command": pytest_command,
            "wrapped": False,
            "completed_at": (BASE_TIME + timedelta(seconds=1)).isoformat(),
            "outcome": "success",
            "exit_code": 0,
        },
        {
            "category": "test",
            "command": quality_command,
            "core_command": quality_command,
            "wrapped": False,
            "completed_at": (BASE_TIME + timedelta(seconds=2)).isoformat(),
            "outcome": "success",
            "exit_code": 0,
        },
    ]


def test_prefixed_success_is_credited_by_core_command() -> None:
    core_command = "uv run pytest tests/tasks/test_close_checklist.py -q"
    command = f"cd /repo && DATABASE_URL=postgres://test {core_command}"

    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1, command=command),)),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"
    assert gate.details["latest_runs"] == [
        {
            "category": "test",
            "command": command,
            "core_command": core_command,
            "wrapped": False,
            "completed_at": (BASE_TIME + timedelta(seconds=1)).isoformat(),
            "outcome": "success",
            "exit_code": 0,
        }
    ]


def test_wrapped_success_does_not_satisfy_validation_gate() -> None:
    command = "uv run pytest tests/tasks/test_close_checklist.py -q | tail -1"

    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1, command=command),)),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["latest_outcomes"] == {}
    assert gate.details["latest_runs"] == [
        {
            "category": "test",
            "command": command,
            "core_command": None,
            "wrapped": True,
            "completed_at": (BASE_TIME + timedelta(seconds=1)).isoformat(),
            "outcome": "success",
            "exit_code": 0,
        }
    ]
    assert gate.details["uncredited_runs"] == [
        {"command": command, "reason": "wrapped", "wrapper_reason": "pipeline"}
    ]


def test_uncredited_runs_explain_unknown_wrapped_and_stale_runs() -> None:
    stale_command = "uv run pytest tests/tasks/test_old.py -q"
    unknown_command = "uv run pytest tests/tasks/test_unknown.py -q"
    wrapped_command = "uv run pytest tests/tasks/test_wrapped.py -q && echo done"
    credited_command = "uv run pytest tests/tasks/test_current.py -q"

    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _run(1, command=stale_command),
                _run(3, outcome="unknown", command=unknown_command),
                _run(4, command=wrapped_command),
                _run(5, command=credited_command),
            ),
            edits=(_edit(2),),
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"
    assert gate.details["uncredited_runs"] == [
        {"command": stale_command, "reason": "stale after a later task edit"},
        {"command": unknown_command, "reason": "unknown outcome"},
        {
            "command": wrapped_command,
            "reason": "wrapped",
            "wrapper_reason": "trailing echo",
        },
    ]


def test_unresolved_failure_blocks_even_when_required_category_passed() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _run(1, categories=("test",)),
                _run(2, outcome="failure", categories=("lint",), command="ruff check"),
            )
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["unresolved_failure_categories"] == ["lint"]
    assert "Re-run each category clean" in gate.message


def test_compound_failure_names_attributed_command_and_timestamp() -> None:
    command = (
        "uv run ruff format --check src/example.py && "
        "uv run ruff check src/example.py && "
        "uv run mypy src/example.py && "
        "uv run pytest tests/test_example.py -q"
    )
    compound = replace(
        _run(
            2,
            outcome="failure",
            categories=("format", "lint", "type_check", "test"),
            command=command,
        ),
        output="FAILED tests/test_example.py::test_example\n1 failed in 0.10s",
        validation_segments=(
            TranscriptValidationSegment(
                command="ruff format --check src/example.py", categories=("format",)
            ),
            TranscriptValidationSegment(
                command="ruff check src/example.py", categories=("lint", "type_check")
            ),
            TranscriptValidationSegment(
                command="mypy src/example.py", categories=("lint", "type_check")
            ),
            TranscriptValidationSegment(
                command="pytest tests/test_example.py -q", categories=("test",)
            ),
        ),
    )

    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(compound,)),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["latest_outcomes"] == {
        "format": "success",
        "lint": "success",
        "test": "failure",
        "type_check": "success",
    }
    assert gate.details["unresolved_failure_categories"] == ["test"]
    assert gate.details["unresolved_failures"] == [
        {
            "category": "test",
            "command": "pytest tests/test_example.py -q",
            "completed_at": "2026-07-27T12:00:02+00:00",
        }
    ]
    assert "pytest tests/test_example.py -q" in gate.message
    assert "2026-07-27T12:00:02+00:00" in gate.message


def test_unattributable_compound_failure_charges_every_segment() -> None:
    command = "uv run ruff format --check src/example.py; uv run pytest tests/test_example.py -q"
    compound = replace(
        _run(2, outcome="failure", categories=("format", "test"), command=command),
        output="validation failed without runner-specific output",
        validation_segments=(
            TranscriptValidationSegment(
                command="ruff format --check src/example.py", categories=("format",)
            ),
            TranscriptValidationSegment(
                command="pytest tests/test_example.py -q", categories=("test",)
            ),
        ),
    )

    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(compound,)),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["latest_outcomes"] == {"format": "failure", "test": "failure"}
    assert gate.details["unresolved_failures"] == [
        {
            "category": "format",
            "command": "ruff format --check src/example.py",
            "completed_at": "2026-07-27T12:00:02+00:00",
        },
        {
            "category": "test",
            "command": "pytest tests/test_example.py -q",
            "completed_at": "2026-07-27T12:00:02+00:00",
        },
    ]
    assert "ruff format --check src/example.py" in gate.message
    assert "pytest tests/test_example.py -q" in gate.message
    assert "2026-07-27T12:00:02+00:00" in gate.message


def test_single_validation_segment_compound_failure_charges_that_segment() -> None:
    command = "uv run pytest tests/test_example.py -q && false"
    compound = replace(
        _run(2, outcome="failure", categories=("test",), command=command),
        output="command failed without runner-specific output",
        validation_segments=(
            TranscriptValidationSegment(
                command="pytest tests/test_example.py -q", categories=("test",)
            ),
        ),
    )

    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(compound,)),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["latest_outcomes"] == {"test": "failure"}
    assert gate.details["unresolved_failures"] == [
        {
            "category": "test",
            "command": "pytest tests/test_example.py -q",
            "completed_at": "2026-07-27T12:00:02+00:00",
        }
    ]


def test_later_clean_categories_clear_unattributable_compound_failure() -> None:
    command = "uv run ruff check src/example.py && uv run mypy src/example.py"
    ruff_segment = TranscriptValidationSegment(
        command="ruff check src/example.py", categories=("lint", "type_check")
    )
    mypy_segment = TranscriptValidationSegment(
        command="mypy src/example.py", categories=("lint", "type_check")
    )
    compound = replace(
        _run(2, outcome="failure", categories=("lint", "type_check"), command=command),
        output="validation failed without runner-specific output",
        validation_segments=(ruff_segment, mypy_segment),
    )
    clean_ruff = replace(
        _run(3, categories=ruff_segment.categories, command="ruff check src/"),
        validation_segments=(
            TranscriptValidationSegment(
                command="ruff check src/", categories=("lint", "type_check")
            ),
        ),
    )
    clean_mypy = replace(
        _run(4, categories=mypy_segment.categories, command="mypy src/"),
        validation_segments=(
            TranscriptValidationSegment(command="mypy src/", categories=("lint", "type_check")),
        ),
    )

    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(compound, clean_ruff, clean_mypy, _run(5, categories=("test",)))
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"
    assert gate.details["unresolved_failure_categories"] == []
    assert gate.details["latest_outcomes"] == {
        "lint": "success",
        "test": "success",
        "type_check": "success",
    }


def test_edit_after_clean_run_makes_validation_stale() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1),),
            edits=(_edit(2),),
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["fresh_run_count"] == 0
    assert "after the final task edit" in gate.message


def test_commit_after_clean_run_is_neutral() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"


def test_clean_run_after_edit_restores_freshness() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1), _run(3)),
            edits=(_edit(2),),
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"


def test_cross_session_evidence_uses_global_ordering() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1), replace(_run(3), session_id="session-2")),
            edits=(replace(_edit(2), session_id="session-2"),),
            sessions=("session-1", "session-2"),
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "passed"
    assert gate.details["sessions"] == ["session-1", "session-2"]


def test_unknown_outcome_never_satisfies_or_blocks_and_names_cure() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1, outcome="unknown"),),
            degraded_capabilities=("codex outcome omitted an exit code",),
        ),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["unresolved_failure_categories"] == []
    assert "unknown results neither satisfy nor block" in gate.message
    assert "definitive exit status" in gate.message


def test_first_failed_gate_stops_ordered_checklist() -> None:
    checklist = first_failed_gate(
        (
            CloseGateResult(1, "task", "passed", "exists"),
            CloseGateResult(2, "session", "failed", "missing"),
            CloseGateResult(3, "repo", "passed", "exists"),
        )
    )

    assert checklist.ready is False
    assert [gate.item for gate in checklist.gates] == [1, 2]
    assert checklist.first_failure is not None
    assert checklist.first_failure.name == "session"
