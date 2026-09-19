from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
import gobby.mcp_proxy.tools.tasks._lifecycle_validation as lifecycle_validation
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import _evaluate_close
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.mcp_proxy.tools.tasks._task_scope import TaskScopeEvaluation
from gobby.storage.tasks import Task
from gobby.tasks.acceptance_artifacts import AcceptanceArtifactResult
from gobby.tasks.close_checklist import (
    CloseChecklist,
    CloseGateResult,
    evaluate_validation_commands,
)
from gobby.tasks.command_equivalence import scope_difference
from gobby.tasks.transcript_evidence import merge_transcript_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
    TranscriptValidationSegment,
)

BASE_TIME = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
EvidenceOutcome = Literal["success", "failure", "unknown"]


@pytest.mark.parametrize(
    ("required", "executed"),
    [
        ("uv run pytest tests/a.py -q", "CI=1 rtk uv run pytest 'tests/a.py' -q > /tmp/out 2>&1"),
        ("uv run pytest tests/a.py -q", "cd '/repo path' && uv run pytest tests/a.py -q >> out"),
        ("uv run pytest tests/a.py -q", "uv run pytest tests/a.py tests/b.py -q"),
        ("uv run pytest tests/a.py::test_one -q", "uv run pytest tests/ -q"),
        ("uv run ruff check src/a.py", "uv run ruff check src/ tests/"),
        ("uv run ruff check src", "uv run ruff check src tests"),
        ("pytest tests/a.py -q", "pytest . -q"),
        ("cargo fmt --all -- --check", "cargo fmt --all --check"),
        ("cargo test -p gobby-core", "cargo test --package=gobby-core"),
        ("cargo clippy -p gobby-core -- -D warnings", "cargo clippy -pgobby-core -- -Dwarnings"),
        ("pytest tests/a.py -k 'a or b'", 'pytest "tests/a.py" -k "a or b" 2>errors'),
        ("pytest tests/a.py -k '|'", 'pytest tests/a.py -k "|"'),
        ("pytest tests/*.py", "pytest tests/*.py"),
        ("uv run pytest tests/a.py tests/b.py", "uv run pytest tests/a.py tests/b.py -q"),
        ("uv run pytest tests/a.py -q", "uv run pytest tests/a.py"),
        ("pytest tests/a.py", "pytest --tb=short --no-header --color yes -rA tests/a.py"),
        ("pytest tests/a.py", "pytest tests/a.py -x --maxfail 2 --durations=5 -vv"),
        ("pytest -ra tests/a.py", "pytest -ra tests/"),
        ("pytest tests/a.py -k foo -p no:randomly", "pytest -p no:randomly tests/a.py -k foo"),
        ("uv run pytest -k close_gate", "uv run pytest -q -k close_gate"),
    ],
)
def test_equivalent_criterion_execution_is_credited(required: str, executed: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1, command=executed),)),
        has_attributed_edits=True,
        validation_criteria=f"Run `{required}`.",
    )
    assert gate.status == "passed", gate.message
    assert gate.details["criterion_command_gaps"] == []


@pytest.mark.parametrize(
    "executed",
    [
        "uv run pytest tests/a.py -q",
        "uv run pytest tests/ -q -k selected",
        "uv run pytest tests/ -q --ignore tests/b.py",
        "uv run pytest tests/ -q | tail -1",
        "uv run pytest tests/ -q || true",
        "uv run pytest tests/ -q & wait",
        "bash -c 'uv run pytest tests/ -q'",
        "uv run pytest tests/ -q >",
        "uv run pytest tests/ -q < input",
        "uv run pytest tests/ -q > $(echo out)",
        "uv run pytest tests/ -q && bash -c 'true'",
        "uv run pytest tests/ -q --collect-only",
        "uv run pytest tests/ -q -m slow",
        "uv run pytest tests/ -q -p no:cacheprovider",
        "uv run pytest -q",
    ],
)
def test_narrowed_or_obscured_criterion_execution_cannot_pass(executed: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1, command=executed),)),
        has_attributed_edits=True,
        validation_criteria="Run `uv run pytest tests/ -q`.",
    )
    assert gate.status == "failed"
    assert "Run `uv run pytest tests/ -q` clean" in gate.message


def test_scope_mismatch_names_the_differing_arguments() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _run(1, command="uv run pytest tests/a.py -k selected"),
                _run(2, command="uv run pytest tests/b.py --tb=short"),
            )
        ),
        has_attributed_edits=True,
        validation_criteria="Run `uv run pytest tests/a.py tests/c.py`.",
    )
    assert gate.status == "failed"
    assert "adds `-k selected`; does not cover `tests/c.py`" in gate.message
    assert "does not cover `tests/a.py tests/c.py`" in gate.message


@pytest.mark.parametrize(
    ("executed", "required", "difference"),
    [
        ("uv run pytest tests/a.py -p x -p x", "uv run pytest tests/a.py -p x", "adds `-p x`"),
        (
            "npx vitest run src/b.test.ts src/a.test.ts",
            "npx vitest run src/a.test.ts src/b.test.ts",
            "orders arguments differently",
        ),
    ],
)
def test_scope_difference_names_repeats_and_reordering(
    executed: str, required: str, difference: str
) -> None:
    assert scope_difference(executed, required) == difference


@pytest.mark.parametrize("outcome", ["failure", "unknown"])
def test_broader_execution_requires_definitive_success(outcome: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_run(1, command="pytest tests/ -q", outcome=outcome),)
        ),
        has_attributed_edits=True,
        validation_criteria="Run `pytest tests/a.py -q`.",
    )
    assert gate.status == "failed"
    assert "Observed `pytest tests/ -q`" in gate.message


@pytest.mark.parametrize(
    ("required", "executed"),
    [
        ("pytest 'tests/*.py'", "pytest tests/*.py"),
        ("pytest tests/*.py", "pytest 'tests/*.py'"),
        ("pytest tests/ -k '\"$VALUE\"'", "pytest tests/ -k \"'$VALUE'\""),
    ],
)
def test_shell_expansion_is_not_literal_argument_equivalence(required: str, executed: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1, command=executed),)),
        has_attributed_edits=True,
        validation_criteria=f"Run `{required}`.",
    )
    assert gate.status == "failed"


@pytest.mark.parametrize(
    "criteria",
    [
        "`cargo test` is not applicable. Run `pytest`.",
        "No need to run `cargo test`; `pytest` must pass.",
        "`cargo test` is unnecessary, but `pytest` is required.",
        "Do not run `cargo test` and run `pytest`.",
        "`cargo test` can be skipped. `pytest` is required.",
        "Run `pytest` (`cargo test` is not applicable).",
        "`cargo test` isn't required BUT `pytest` is required.",
    ],
)
def test_exclusion_is_local_to_the_command_clause(criteria: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
        validation_criteria=criteria,
    )
    assert gate.status == "passed", gate.message
    assert [record["command"] for record in gate.details["criterion_commands"]] == ["pytest"]


def test_excluded_occurrence_does_not_hide_a_later_required_occurrence() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
        validation_criteria="`cargo test` is not required before editing. Run `cargo test` afterward.",
    )
    assert gate.status == "failed"
    assert "Run `cargo test` clean" in gate.message


@pytest.mark.parametrize(
    "span",
    [
        "gobby start",
        "gobby start --verbose",
        "gobby stop",
        "gobby restart",
        "gobby restart --wait",
        "gobby cutover",
        "uv run gobby restart",
        "uv run gobby restart --wait",
        "uv run gobby cutover --allow-dirty",
        "uv run --frozen gobby restart --wait",
        "GOBBY_ALLOW_WORKTREE_DAEMON=1 gobby start",
    ],
)
def test_daemon_lifecycle_criterion_spans_never_block_close(span: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
        validation_criteria=f"The coordinator runs `{span}` after the merge.",
    )
    assert gate.status == "passed", gate.message
    assert gate.details["criterion_commands"] == []
    assert gate.details["criterion_command_gaps"] == []


def test_live_criteria_produce_no_mandatory_criterion_commands() -> None:
    criteria = (
        "- Focused tests pass.\n"
        "- Live: `gobby restart` succeeds after the merge.\n"
        "- Live: `uv run pytest tests/smoke.py` passes."
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
        validation_criteria=criteria,
    )
    assert gate.status == "passed", gate.message
    assert gate.details["criterion_commands"] == []
    assert gate.details["criterion_command_gaps"] == []


def test_non_lifecycle_gobby_criterion_command_still_blocks_close() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
        validation_criteria="Run `gobby test-types audit tests/` clean.",
    )
    assert gate.status == "failed"
    assert [record["command"] for record in gate.details["criterion_commands"]] == [
        "gobby test-types audit tests/"
    ]


def test_all_unmet_criteria_report_observed_wrappers_scope_and_stale_edits() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _run(1, command="cargo fmt --all --check"),
                _run(3, command="pytest tests/a.py -q"),
                _run(4, command="pytest tests/ -q | tail -1"),
            ),
            edits=(_edit(2),),
        ),
        has_attributed_edits=True,
        validation_criteria="Run `cargo fmt --all -- --check` and `pytest tests/ -q`.",
    )
    assert gate.status == "failed"
    assert "cargo fmt --all --check" in gate.message
    assert "invalidated by src/example.py" in gate.message
    assert "Observed `pytest tests/a.py -q`: scope or semantic arguments differ" in gate.message
    assert "Observed `pytest tests/ -q | tail -1`: pipeline" in gate.message


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
                languages=("python",),
                bounded_inputs=True,
            ),
        ),
    )


def _scoped_audit_run(order: int, *targets: str) -> TranscriptValidationRun:
    normalized_command = (
        f"gobby test-types audit {' '.join(targets)} "
        "--baseline .gobby/test-types-baseline.json --fail-on-new"
    )
    return _audit_run(
        order,
        command=f"uv run {normalized_command}",
        normalized_command=normalized_command,
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


def test_python_test_change_requires_covering_test_types_audit() -> None:
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
    assert gate.details["test_types_audit_uncovered_paths"] == [
        "tests/tasks/test_close_checklist.py"
    ]
    assert (
        "uv run gobby test-types audit tests/ "
        "--baseline .gobby/test-types-baseline.json --fail-on-new"
    ) in gate.message


@pytest.mark.parametrize(
    ("target", "normalized_target"),
    [
        ("tests/tasks/test_close_checklist.py", "tests/tasks/test_close_checklist.py"),
        ("tests/tasks/", "tests/tasks"),
        ("./tests/tasks/../tasks", "tests/tasks"),
    ],
)
def test_scoped_test_types_audit_covering_changed_test_passes(
    target: str,
    normalized_target: str,
) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_scoped_audit_run(1, target), _run(2))),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "passed"
    assert gate.details["test_types_audit_targets"] == [normalized_target]
    assert gate.details["test_types_audit_uncovered_paths"] == []


def test_partial_test_types_audit_names_exact_uncovered_paths() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _scoped_audit_run(1, "tests/tasks/test_close_checklist.py"),
                _run(2),
            )
        ),
        has_attributed_edits=True,
        changed_paths=(
            "tests/tasks/test_close_checklist.py",
            "tests/tasks/test_transcript_evidence.py",
        ),
    )

    assert gate.status == "failed"
    assert gate.details["test_types_audit_uncovered_paths"] == [
        "tests/tasks/test_transcript_evidence.py"
    ]
    assert "`tests/tasks/test_transcript_evidence.py`" in gate.message


def test_multiple_explicit_test_types_targets_cover_changed_tests() -> None:
    changed_paths = (
        "tests/tasks/test_close_checklist.py",
        "tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py",
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_scoped_audit_run(1, *changed_paths), _run(2))
        ),
        has_attributed_edits=True,
        changed_paths=changed_paths,
    )

    assert gate.status == "passed"
    assert gate.details["test_types_audit_targets"] == list(changed_paths)


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


def test_bounded_audit_survives_later_edit_in_other_language() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_audit_run(1), _run(3)),
            edits=(replace(_edit(2), path="web/src/fixtures.ts"),),
            edit_languages={"web/src/fixtures.ts": "typescript"},
        ),
        has_attributed_edits=True,
        validation_criteria=(
            "Run `uv run gobby test-types audit tests/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new`."
        ),
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "passed"
    assert gate.details["latest_test_types_audit"]["outcome"] == "success"
    assert gate.details["criterion_commands"][0]["status"] == "satisfied"
    assert gate.details["uncredited_runs"] == []
    assert gate.details["excluded_runs"] == []


@pytest.mark.parametrize(
    ("path", "languages"),
    [
        ("tests/tasks/test_close_checklist.py", {"tests/tasks/test_close_checklist.py": "python"}),
        (".github/workflows/ci.yml", {".github/workflows/ci.yml": "yaml"}),
        (".gobby/test-types-baseline.json", {".gobby/test-types-baseline.json": "json"}),
        ("pyproject.toml", {}),
    ],
)
def test_bounded_audit_is_stale_after_affecting_edit(path: str, languages: dict[str, str]) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_audit_run(1), _run(4)),
            edits=(replace(_edit(2), path="web/src/app.ts"), replace(_edit(3), path=path)),
            edit_languages={"web/src/app.ts": "typescript", **languages},
        ),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "failed"
    assert gate.details["latest_test_types_audit"] is None
    (stale,) = [
        record for record in gate.details["excluded_runs"] if record["reason_code"] == "stale"
    ]
    assert stale["invalidating_edit"]["path"] == path
    assert [record["invalidating_edit"]["path"] for record in gate.details["uncredited_runs"]] == [
        path
    ]


def test_test_category_and_compound_runs_stay_globally_stale() -> None:
    test_run = replace(
        _run(1, command="uv run pytest tests/test_x.py"),
        validation_segments=(
            TranscriptValidationSegment(
                command="pytest tests/test_x.py",
                categories=("test",),
                languages=("python",),
                bounded_inputs=True,
            ),
        ),
    )
    compound = replace(
        _run(
            2,
            categories=("lint", "type_check"),
            command="uv run ruff check src/ && uv run mypy src/",
        ),
        validation_segments=(
            TranscriptValidationSegment(
                command="ruff check src/",
                categories=("lint", "type_check"),
                languages=("python",),
                bounded_inputs=True,
            ),
            TranscriptValidationSegment(
                command="mypy src/",
                categories=("lint", "type_check"),
                segment_index=1,
                languages=("python",),
                bounded_inputs=True,
            ),
        ),
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(test_run, compound),
            edits=(replace(_edit(3), path="web/src/app.ts"),),
            edit_languages={"web/src/app.ts": "typescript"},
        ),
        has_attributed_edits=True,
        validation_criteria="Run `uv run mypy src/`.",
    )

    assert gate.status == "failed"
    assert gate.details["fresh_run_count"] == 0
    assert [record["reason_code"] for record in gate.details["excluded_runs"]] == ["stale", "stale"]
    assert [record["status"] for record in gate.details["criterion_commands"]] == ["stale"]


@pytest.mark.parametrize(
    ("command", "normalized_command"),
    [
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
    "normalized_command",
    [
        "gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json "
        "--fail-on-new --format json",
        "gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json "
        "--fail-on-new --format=json",
        "gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json "
        "--fail-on-new --min-severity low",
        "gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json "
        "--fail-on-new --output .gobby/audit.txt",
        "gobby test-types audit --format json --output .gobby/audit.json "
        "--min-severity low --baseline .gobby/test-types-baseline.json --fail-on-new "
        "tests/tasks/test_close_checklist.py",
    ],
)
def test_output_only_flags_keep_test_types_audit_credit(normalized_command: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _audit_run(
                    1,
                    command=f"uv run {normalized_command}",
                    normalized_command=normalized_command,
                ),
                _run(2),
            )
        ),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "passed"
    assert gate.details["test_types_audit_uncovered_paths"] == []
    assert gate.details["latest_test_types_audit"]["outcome"] == "success"


@pytest.mark.parametrize(
    "normalized_command",
    [
        # An output-only flag left without its value is a malformed invocation.
        "gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json "
        "--fail-on-new --format",
        # `--mypy-command` changes what the audit enforces, so it is not output-only.
        "gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json "
        "--fail-on-new --mypy-command 'mypy --no-error-summary'",
        # A near-miss spelling must not be credited by prefix.
        "gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json "
        "--fail-on-new --format-json",
    ],
)
def test_nonoutput_flags_still_void_test_types_audit_credit(normalized_command: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _audit_run(
                    1,
                    command=f"uv run {normalized_command}",
                    normalized_command=normalized_command,
                ),
                _run(2),
            )
        ),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "failed"
    assert gate.details["latest_test_types_audit"] is None


def test_output_only_flag_value_is_not_read_as_an_audit_target() -> None:
    """The consumed `--output` value must not stand in for a missing test target."""
    normalized_command = (
        "gobby test-types audit --output tests/tasks --baseline "
        ".gobby/test-types-baseline.json --fail-on-new"
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(
                _audit_run(
                    1,
                    command=f"uv run {normalized_command}",
                    normalized_command=normalized_command,
                ),
                _run(2),
            )
        ),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_close_checklist.py",),
    )

    assert gate.status == "failed"
    assert gate.details["latest_test_types_audit"] is None
    assert gate.details["test_types_audit_targets"] == []


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


def test_parent_directory_audit_covers_deleted_python_test() -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(
            validation_runs=(_scoped_audit_run(1, "tests/tasks/"), _run(2))
        ),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_deleted.py",),
    )

    assert gate.status == "passed"


def test_rename_requires_audit_to_cover_source_and_destination() -> None:
    destination = "tests/tasks/test_new_name.py"
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_scoped_audit_run(1, destination), _run(2))),
        has_attributed_edits=True,
        changed_paths=("tests/tasks/test_old_name.py", destination),
    )

    assert gate.status == "failed"
    assert gate.details["test_types_audit_uncovered_paths"] == ["tests/tasks/test_old_name.py"]


@pytest.mark.parametrize(
    "changed_path",
    ["src/gobby/tasks/close_checklist.py", "tests/test_close_checklist.ts"],
)
def test_non_python_test_changes_keep_existing_validation_behavior(changed_path: str) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1),)),
        has_attributed_edits=True,
        changed_paths=(changed_path,),
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
    stale = [
        run
        for run in gate.details["uncredited_runs"]
        if run["reason"] == "stale after a later task edit"
    ]
    assert [run["core_command"] for run in stale] == ([] if rerun else ["npm ci"])
    if stale:
        assert stale[0]["order"] == 1
        assert stale[0]["invalidating_edit"]["path"] == "src/example.py"
    assert {"command": "unrecognized-check", "reason": "unknown outcome"} in gate.details[
        "uncredited_runs"
    ]
    wrapped = next(run for run in gate.details["latest_runs"] if run["wrapped"])
    assert wrapped["command"] == "npm ci; echo done"
    assert wrapped["core_command"] is None
    assert any(item["reason"] == "wrapped" for item in gate.details["uncredited_runs"])
    credited = [run for run in gate.details["latest_runs"] if not run["wrapped"]]
    assert [run["core_command"] for run in credited] == (["npm ci"] if rerun else [])


def test_fresh_equivalent_rerun_replaces_stale_prefixed_execution() -> None:
    command = "uv run pytest tests/tasks/test_close_checklist.py -q"
    evidence = TranscriptEvidence(
        validation_runs=(
            _run(1, command=f"GOBBY_TEST_PROTECT=1 {command}"),
            _run(3, command=f"DATABASE_URL=postgresql://test {command}"),
        ),
        edits=(_edit(2),),
    )

    gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
        validation_criteria=f"`CI=1 {command}` passes.",
    )

    assert gate.status == "passed"
    assert gate.details["criterion_commands"] == [
        {
            "command": f"CI=1 {command}",
            "core_command": command,
            "status": "satisfied",
            "satisfied": True,
            "execution": {
                "session_id": "session-1",
                "source": "codex",
                "command": f"DATABASE_URL=postgresql://test {command}",
                "core_command": command,
                "completed_at": (BASE_TIME + timedelta(seconds=3)).isoformat(),
                "order": 3,
                "outcome": "success",
                "exit_code": 0,
            },
        }
    ]
    assert not any(
        run["reason"] == "stale after a later task edit" and run["core_command"] == command
        for run in gate.details["uncredited_runs"]
    )


def test_criterion_command_gaps_are_aggregated_with_causal_diagnostics() -> None:
    stale_command = "uv run pytest tests/tasks/test_close_checklist.py -q"
    missing_command = "uv run ruff check src/gobby/tasks/close_checklist.py"
    evidence = TranscriptEvidence(
        validation_runs=(_run(1, command=f"GOBBY_TEST_PROTECT=1 {stale_command}"),),
        edits=(_edit(2),),
    )

    gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
        validation_criteria=(
            f"`DATABASE_URL=postgresql://test {stale_command}` passes. `{missing_command}` passes."
        ),
    )

    assert gate.status == "failed"
    assert gate.message.count("Run `") == 2
    assert stale_command in gate.message
    assert missing_command in gate.message
    gaps = gate.details["criterion_command_gaps"]
    assert [(gap["core_command"], gap["status"]) for gap in gaps] == [
        (stale_command, "stale"),
        (missing_command, "missing"),
    ]
    # Gaps are compact references; the execution record stays in criterion_commands.
    assert all(set(gap) == {"command", "core_command", "status"} for gap in gaps)
    unsatisfied = [
        record for record in gate.details["criterion_commands"] if not record["satisfied"]
    ]
    stale_execution = unsatisfied[0]["execution"]
    assert stale_execution["order"] == 1
    assert stale_execution["completed_at"] == (BASE_TIME + timedelta(seconds=1)).isoformat()
    assert stale_execution["invalidating_edit"] == {
        "session_id": "session-1",
        "source": "codex",
        "path": "src/example.py",
        "timestamp": (BASE_TIME + timedelta(seconds=2)).isoformat(),
        "order": 2,
        "tool_name": "apply_patch",
    }


@pytest.mark.parametrize(
    ("command", "outcome", "status"),
    [
        ("uv run pytest tests/x.py -q | tail -1", "success", "wrapped"),
        ("uv run pytest tests/x.py -q", "unknown", "unknown"),
    ],
)
def test_criterion_commands_keep_wrappers_and_unknown_outcomes_uncredited(
    command: str,
    outcome: str,
    status: str,
) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1, command=command, outcome=outcome),)),
        has_attributed_edits=True,
        validation_criteria=f"`{command}` passes.",
    )

    assert gate.status == "failed"
    assert gate.details["criterion_command_gaps"][0]["status"] == status
    assert gate.details["uncredited_runs"][0]["reason"] in {"wrapped", "unknown outcome"}


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


def test_successful_top_level_and_segments_are_credited_individually() -> None:
    test_command = "uv run pytest tests/tasks/test_close_checklist.py -q"
    lint_command = "uv run ruff check src/gobby/tasks/close_checklist.py"
    command = f"GOBBY_TEST_PROTECT=1 {test_command} && {lint_command}"
    compound = replace(
        _run(1, categories=("test", "lint", "type_check"), command=command),
        validation_segments=(
            TranscriptValidationSegment(
                command="pytest tests/tasks/test_close_checklist.py -q",
                categories=("test",),
                segment_index=0,
            ),
            TranscriptValidationSegment(
                command="ruff check src/gobby/tasks/close_checklist.py",
                categories=("lint", "type_check"),
                segment_index=1,
            ),
        ),
    )

    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(compound,)),
        has_attributed_edits=True,
        validation_criteria=f"`{test_command}` and `{lint_command}` pass.",
    )

    assert gate.status == "passed"
    assert gate.details["latest_outcomes"] == {
        "lint": "success",
        "test": "success",
        "type_check": "success",
    }
    assert gate.details["latest_runs"] == [
        {
            "category": "test",
            "command": f"GOBBY_TEST_PROTECT=1 {test_command}",
            "core_command": test_command,
            "wrapped": False,
            "completed_at": (BASE_TIME + timedelta(seconds=1)).isoformat(),
            "outcome": "success",
            "exit_code": 0,
        },
        {
            "category": "lint",
            "command": lint_command,
            "core_command": lint_command,
            "wrapped": False,
            "completed_at": (BASE_TIME + timedelta(seconds=1)).isoformat(),
            "outcome": "success",
            "exit_code": 0,
        },
    ]
    assert [record["status"] for record in gate.details["criterion_commands"]] == [
        "satisfied",
        "satisfied",
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


@pytest.mark.parametrize(
    ("command", "wrapper_reason"),
    [
        ("pytest tests/x.py || true", "fallback"),
        ("pytest tests/x.py & wait", "backgrounding"),
        ("pytest tests/x.py && printf done", "trailing printf"),
        ("pytest tests/x.py; true", "command sequence"),
    ],
)
def test_uncredited_compound_success_reports_specific_wrapper_reason(
    command: str,
    wrapper_reason: str,
) -> None:
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(_run(1, command=command),)),
        has_attributed_edits=True,
    )

    assert gate.status == "failed"
    assert gate.details["latest_outcomes"] == {}
    assert gate.details["uncredited_runs"] == [
        {"command": command, "reason": "wrapped", "wrapper_reason": wrapper_reason}
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
    stale, unknown, wrapped = gate.details["uncredited_runs"]
    assert stale["command"] == stale_command
    assert stale["reason"] == "stale after a later task edit"
    assert stale["order"] == 1
    assert stale["completed_at"] == (BASE_TIME + timedelta(seconds=1)).isoformat()
    assert stale["invalidating_edit"]["path"] == "src/example.py"
    assert stale["invalidating_edit"]["order"] == 2
    assert unknown == {"command": unknown_command, "reason": "unknown outcome"}
    assert wrapped == {
        "command": wrapped_command,
        "reason": "wrapped",
        "wrapper_reason": "trailing echo",
    }


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


def test_all_failures_reports_every_blocker_and_keeps_the_first_for_the_reason() -> None:
    """One evaluation names every failed gate; the first stays the headline reason."""
    checklist = CloseChecklist(
        (
            CloseGateResult(1, "task_exists", "passed", "exists"),
            CloseGateResult(6, "changes_summary_present", "failed", "summary missing"),
            CloseGateResult(7, "linked_commits", "passed", "linked"),
            CloseGateResult(9, "uncommitted_task_edits", "failed", "src/a.py is dirty"),
            CloseGateResult(
                11, "acceptance_artifacts", "skipped", "Not evaluated because gate X failed."
            ),
        )
    )

    assert checklist.ready is False
    assert [gate.name for gate in checklist.all_failures] == [
        "changes_summary_present",
        "uncommitted_task_edits",
    ]
    assert checklist.first_failure is not None
    assert checklist.first_failure.name == "changes_summary_present"


def test_all_failures_is_empty_when_only_skips_remain() -> None:
    """A skipped gate is unevaluated, so it never becomes a blocker to fix."""
    checklist = CloseChecklist(
        (
            CloseGateResult(1, "task_exists", "passed", "exists"),
            CloseGateResult(12, "tdd_evidence", "skipped", "Not evaluated because gate X failed."),
        )
    )

    assert checklist.all_failures == ()
    assert checklist.first_failure is None
    assert checklist.ready is True


def test_checklist_summary_drops_the_detail_payloads() -> None:
    """Concise responses carry every gate status without gate 10's kilobytes of detail."""
    checklist = CloseChecklist(
        (
            CloseGateResult(
                10,
                "validation_commands",
                "failed",
                "No clean test run.",
                details={"unresolved_failure_categories": ["test"]},
            ),
        )
    )

    assert checklist.summary() == [
        {
            "item": 10,
            "name": "validation_commands",
            "status": "failed",
            "message": "No clean test run.",
        }
    ]


_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


def _task(*, criteria: str | None = "Focused tests pass.") -> Task:
    return Task(
        id="00000000-0000-4000-8000-000000000101",
        project_id="00000000-0000-4000-8000-000000000201",
        title="Close checklist leaf",
        category="code",
        priority=2,
        task_type="task",
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        claimed_by_session_id="00000000-0000-4000-8000-000000000301",
        validation_criteria=criteria,
        validation_fail_count=0,
        stages=({"stage_name": "development", "position": 0, "state": "in_progress"},),
    )


def _ctx(task: Task) -> RegistryContext:
    """A context of fakes: the gates below read the task row and the call arguments."""
    manager = MagicMock()
    manager.db = MagicMock()
    manager.get_task.return_value = task
    manager.list_tasks.return_value = []
    close_session = SimpleNamespace(id=task.claimed_by_session_id, machine_id=_MACHINE_ID)
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=manager,
            task_validator=object(),
            agent_registry=None,
            project_manager=MagicMock(),
            session_manager=SimpleNamespace(get=lambda _session_id: close_session),
            session_var_manager=SimpleNamespace(get_variables=lambda _session_id: {}),
            validation_config=None,
            resolve_session_id=lambda session_id: session_id,
            get_current_project_name=lambda: "gobby",
        ),
    )


@pytest.mark.asyncio
async def test_every_independent_deterministic_blocker_lands_in_one_response() -> None:
    """Four independent gates fail, so one call names all four.

    Eight feedback observations described fixing one blocker, retrying, and meeting
    the next. The gates below share no inputs, so each verdict stands on its own.
    """
    task = _task()
    ctx = _ctx(task)
    scope = TaskScopeEvaluation(
        declared_paths=("tests/",),
        actual_paths=("src/a.py",),
        out_of_scope_paths=("src/a.py",),
        justification_error="Task changes exceed the declared scope.",
    )
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "collect_commit_paths", return_value=set()),
        patch.object(lifecycle, "unlinked_tagged_commits", return_value=(([], []), None)),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "_committable_task_paths", return_value={"src/a.py"}),
        patch.object(lifecycle_validation, "task_dirty_paths_async", return_value={"src/a.py"}),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=(["abc123"], None)),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(can_close=True),
        ),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(lifecycle, "evaluate_task_scope", AsyncMock(return_value=scope)),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            AsyncMock(return_value=TranscriptEvidence()),
        ),
        patch.object(
            lifecycle,
            "evaluate_acceptance_artifacts",
            AsyncMock(
                return_value=AcceptanceArtifactResult(
                    passed=True, tests=(), findings=(), evidence_files=()
                )
            ),
        ),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch("gobby.workflows.task_claim_state.target_task_has_edits", return_value=True),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/a.py"},
        ),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="",
            commit_sha="abc123",
            project_path=None,
            response_detail="concise",
        )

    failures = evaluation.checklist.all_failures
    assert [gate.name for gate in failures] == [
        "changes_summary_present",
        "task_scope",
        "uncommitted_task_edits",
        "validation_commands",
    ]
    response = evaluation.response(preview=True)
    # The reason stays the first failure for readability; the gate list carries the rest.
    assert response["error"] == "missing_changes_summary"
    assert response["message"] == evaluation.checklist.gates[5].message
    assert len(response["blocking_reasons"]) == 4
    assert [entry["item"] for entry in response["gates"]] == list(range(1, 14))
    assert [entry["item"] for entry in response["gates"] if entry["status"] == "failed"] == [
        6,
        8,
        9,
        10,
    ]
    # A concise response carries the statuses without the gate detail payloads.
    assert "details" not in response["gates"][0]
    assert response["gates"][-1]["status"] == "skipped"
    assert "changes_summary_present" in response["gates"][-1]["message"]
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_dependent_gates_report_skipped_instead_of_a_borrowed_failure() -> None:
    """An unlinked commit set makes gates 11 and 12 unevaluable, never failed.

    Gate 11 resolves each named test body out of the last linked commit, so running it
    here would report a missing test that only the missing commit made unresolvable.
    """
    task = _task(criteria="Acceptance: `tests/test_example.py::test_example` passes.")
    ctx = _ctx(task)
    transcript = TranscriptEvidence(
        validation_runs=(_run(1, command="uv run pytest tests/test_example.py -q"),),
        sessions=("session-1",),
    )
    acceptance = AsyncMock()
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "collect_commit_paths", return_value=set()),
        patch.object(lifecycle, "unlinked_tagged_commits", return_value=(([], []), None)),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "_committable_task_paths", return_value={"src/a.py"}),
        patch.object(lifecycle_validation, "task_dirty_paths_async", return_value=set()),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(
                can_close=False,
                error_type="commit_required",
                message="Link a commit for the attributed task edits.",
            ),
        ),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(
            lifecycle,
            "evaluate_task_scope",
            AsyncMock(return_value=TaskScopeEvaluation((), (), ())),
        ),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            AsyncMock(return_value=transcript),
        ),
        patch.object(lifecycle, "evaluate_acceptance_artifacts", acceptance),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch("gobby.workflows.task_claim_state.target_task_has_edits", return_value=True),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/a.py"},
        ),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented and tested.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.error == "commit_required"
    statuses = {gate.item: gate.status for gate in evaluation.gates}
    assert statuses[7] == "failed"
    # The commit-independent gates still deliver their own verdicts.
    assert statuses[9] == "passed"
    assert statuses[10] == "passed"
    unevaluable = [gate for gate in evaluation.gates if gate.item in {11, 12, 13}]
    assert [gate.status for gate in unevaluable] == ["skipped", "skipped", "skipped"]
    assert all("linked_commits" in gate.message for gate in unevaluable)
    assert [gate.name for gate in evaluation.checklist.all_failures] == ["linked_commits"]
    acceptance.assert_not_awaited()
    review.assert_not_awaited()
