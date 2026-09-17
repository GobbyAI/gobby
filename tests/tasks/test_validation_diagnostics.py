"""Excluded observations explain a failed close without changing its credit."""

from dataclasses import replace

import pytest

from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence import merge_transcript_evidence
from gobby.tasks.transcript_evidence_models import TranscriptEvidence
from tests.tasks.test_close_checklist import _audit_run, _edit, _run

pytestmark = pytest.mark.unit
AUDIT = (
    "uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new"
)


@pytest.mark.parametrize(
    ("shape", "command", "outcome", "exit_code", "reason", "remedy"),
    [
        ("stale", AUDIT, "success", 0, "stale", "after the final task edit"),
        ("prelink", AUDIT, "success", 0, "pre-link", "Link this session"),
        ("run", AUDIT + " | tail -1", "success", 0, "wrapped", "Remove pipes"),
        ("run", AUDIT + " > result.txt", "unknown", None, "unknown", "definitive exit status"),
        ("run", AUDIT + "; echo done", "failure", 1, "wrapped", "run each validation"),
        ("run", AUDIT, "unknown", None, "unknown", "definitive exit status"),
        ("run", AUDIT, "failure", 127, "invocation-failed", "Fix the executable"),
        ("run", AUDIT, "failure", 1, "assertion-failed", "Fix the reported assertions"),
        (
            "run",
            "uv run gobby test-types audit tests/other.py --baseline .gobby/test-types-baseline.json --fail-on-new",
            "success",
            0,
            "partial",
            "tests/test_changed.py",
        ),
    ],
)
def test_audit_exclusions_name_observed_command_and_remedy(
    shape: str, command: str, outcome: str, exit_code: int | None, reason: str, remedy: str
) -> None:
    run = replace(
        _audit_run(
            1, outcome=outcome, command=command, normalized_command=command.removeprefix("uv run ")
        ),
        exit_code=exit_code,
    )
    evidence = TranscriptEvidence(
        validation_runs=() if shape == "prelink" else (run,),
        excluded_runs=(run,) if shape == "prelink" else (),
        edits=(_edit(2),) if shape == "stale" else (),
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
        changed_paths=("tests/test_changed.py",),
    )
    assert gate.status == "failed"
    observed = gate.details["nearest_observed_run"]
    assert observed["command"] == command
    assert observed["completed_at"] == run.completed_at.isoformat()
    assert observed["reason_code"] == reason
    assert remedy in observed["remedy"]
    assert command in gate.message
    assert run.completed_at.isoformat() in gate.message
    assert remedy in gate.message
    assert observed in gate.details["excluded_runs"]


@pytest.mark.parametrize("exit_code", [2, 3, 4, 5, 126, 127])
def test_pytest_invocation_failures_are_not_assertion_failures(exit_code: int) -> None:
    run = replace(
        _run(1, outcome="failure", command="uv run pytest tests/test_changed.py"),
        exit_code=exit_code,
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(run,)),
        has_attributed_edits=True,
    )
    assert gate.status == "failed"
    assert gate.details["nearest_observed_run"]["reason_code"] == "invocation-failed"
    assert str(exit_code) in gate.message


def test_prelink_runs_survive_merge_but_never_satisfy_criteria_or_category() -> None:
    old = _run(1, command="uv run pytest tests/test_changed.py")
    merged = merge_transcript_evidence(TranscriptEvidence(excluded_runs=(old,)))
    assert merged.validation_runs == ()
    assert merged.command_runs == ()
    assert merged.edits == ()
    assert merged.excluded_runs == (old,)
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=merged,
        has_attributed_edits=True,
        validation_criteria="Run `uv run pytest tests/test_changed.py`.",
    )
    assert gate.status == "failed"
    assert gate.details["criterion_commands"][0]["satisfied"] is False
    assert gate.details["nearest_observed_run"]["reason_code"] == "pre-link"


def test_fresh_success_cures_failure_without_crediting_excluded_runs() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        has_attributed_edits=True,
        evidence=TranscriptEvidence(validation_runs=(_run(2),), excluded_runs=(_run(1),)),
    )
    assert gate.status == "passed"
    assert "Observed" not in gate.message
    assert gate.details["validation_run_count"] == 1


def test_nearest_audit_is_selected_instead_of_newer_unrelated_command() -> None:
    audit = _audit_run(1, command=AUDIT + " | tail -1")
    unrelated = _run(2, command="npm ci", outcome="unknown", categories=())
    gate = evaluate_validation_commands(
        task_category="code",
        has_attributed_edits=True,
        changed_paths=("tests/test_changed.py",),
        evidence=TranscriptEvidence(validation_runs=(audit,), command_runs=(unrelated,)),
    )
    assert gate.details["nearest_observed_run"]["command"] == audit.command
    assert "npm ci" not in gate.message


def test_partial_audit_remedy_covers_all_changed_tests_in_one_run() -> None:
    partial_command = AUDIT.replace("tests/", "tests/a.py")
    partial = _audit_run(
        2, command=partial_command, normalized_command=partial_command.removeprefix("uv run ")
    )
    evidence = TranscriptEvidence(validation_runs=(_run(1), partial))
    changed = ("tests/a.py", "tests/b.py")
    gate = evaluate_validation_commands(
        task_category="code",
        has_attributed_edits=True,
        changed_paths=changed,
        evidence=evidence,
    )
    observed = gate.details["nearest_observed_run"]
    assert observed["reason_code"] == "partial"
    assert observed["uncovered_paths"] == ["tests/b.py"]
    command = observed["remedy"].split("`", 2)[1]
    cured = evaluate_validation_commands(
        task_category="code",
        has_attributed_edits=True,
        changed_paths=changed,
        evidence=replace(
            evidence,
            validation_runs=(
                *evidence.validation_runs,
                _audit_run(3, command=command, normalized_command=command.removeprefix("uv run ")),
            ),
        ),
    )
    assert cured.status == "passed", cured.message


def test_partial_audit_remedy_says_how_to_cover_a_deleted_test() -> None:
    """The audit cannot read a deleted file, so the remedy names the parent-directory cure."""
    partial_command = AUDIT.replace("tests/", "tests/a.py")
    partial = _audit_run(
        2, command=partial_command, normalized_command=partial_command.removeprefix("uv run ")
    )
    evidence = TranscriptEvidence(validation_runs=(_run(1), partial))
    changed = ("tests/a.py", "tests/search/test_gone.py")
    gate = evaluate_validation_commands(
        task_category="code",
        has_attributed_edits=True,
        changed_paths=changed,
        evidence=evidence,
    )
    remedy = gate.details["nearest_observed_run"]["remedy"]
    assert "deleted test file" in remedy
    assert "parent directory" in remedy

    parent_command = AUDIT.replace("tests/", "tests/a.py tests/search")
    cured = evaluate_validation_commands(
        task_category="code",
        has_attributed_edits=True,
        changed_paths=changed,
        evidence=replace(
            evidence,
            validation_runs=(
                *evidence.validation_runs,
                _audit_run(
                    3,
                    command=parent_command,
                    normalized_command=parent_command.removeprefix("uv run "),
                ),
            ),
        ),
    )
    assert cured.status == "passed", cured.message


def test_narrowed_criterion_has_observed_metadata_and_exact_remedy() -> None:
    run = _run(1, command="uv run pytest tests/a.py -q")
    required = "uv run pytest tests/ -q"
    gate = evaluate_validation_commands(
        task_category="code",
        has_attributed_edits=True,
        validation_criteria=f"Run `{required}`.",
        evidence=TranscriptEvidence(validation_runs=(run,)),
    )
    observed = gate.details["nearest_observed_run"]
    assert gate.status == "failed"
    assert observed["command"] == run.command
    assert observed["completed_at"] == run.completed_at.isoformat()
    assert observed["reason_code"] == "partial"
    assert f"Run `{required}` clean" in observed["remedy"]
