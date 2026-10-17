"""Git-backed regression tests for landed-candidate validation credit."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.tasks.close_checklist import CloseGateResult, evaluate_validation_commands
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun

TEST = "tests/test_widget.py"
SOURCE = "src/widget.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, value: int) -> str:
    (root / SOURCE).write_text(f"VALUE = {value}\n")
    (root / TEST).write_text(
        f"from src.widget import VALUE\n\ndef test_value() -> None:\n    assert VALUE == {value}\n"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", f"widget {value}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path, str]:
    integration = tmp_path / "integration"
    integration.mkdir()
    _git(integration, "init", "-b", "shipping")
    _git(integration, "config", "user.email", "test@example.invalid")
    _git(integration, "config", "user.name", "Test")
    (integration / "src").mkdir()
    (integration / "tests").mkdir()
    candidate = _commit(integration, 1)
    close = tmp_path / "close"
    _git(integration, "worktree", "add", "--detach", str(close), candidate)
    return integration, close, candidate


def _gate(close: Path, run_tree: Path, candidate: str) -> CloseGateResult:
    now = datetime.now(UTC)
    runs = tuple(
        TranscriptValidationRun(
            session_id="closing-session",
            source="test",
            command=command,
            categories=categories,
            matcher_id=matcher,
            label=matcher,
            outcome="success",
            started_at=now,
            completed_at=now,
            order=order,
            exit_code=0,
            output=output,
            workdir=str(run_tree),
        )
        for order, command, categories, matcher, output in (
            (
                1,
                f"uv run gobby test-types audit {TEST} "
                "--baseline .gobby/test-types-baseline.json --fail-on-new",
                ("type_check",),
                "gobby-test-types-audit",
                "0 errors",
            ),
            (2, f"uv run pytest {TEST} -q", ("test",), "pytest", "1 passed in 0.01s"),
        )
    )
    return evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=runs),
        has_attributed_edits=True,
        changed_paths=(TEST, SOURCE),
        close_root=str(close),
        candidate_commit_sha=candidate,
    )


@pytest.mark.parametrize("close_at_tip", [False, True])
def test_landed_candidate_credits_clean_integration_tip_run(
    repository: tuple[Path, Path, str], close_at_tip: bool
) -> None:
    integration, close, candidate = repository
    _commit(integration, 2)
    gate = _gate(integration if close_at_tip else close, integration, candidate)
    assert gate.status == "passed", gate.message
    assert gate.details["pytest_uncovered_paths"] == []
    assert gate.details["python_source_uncovered_tests"] == {}


@pytest.mark.parametrize(
    ("case", "reason"),
    [("unlanded", "candidate is not landed"), ("dirty", "dirty"), ("side", "not on")],
)
def test_unlanded_or_dirty_tree_run_stays_uncredited(
    repository: tuple[Path, Path, str], case: str, reason: str
) -> None:
    integration, close, candidate = repository
    if case == "unlanded":
        _git(close, "switch", "-c", "candidate")
        candidate = _commit(close, 3)
        _commit(integration, 2)
        run_tree = integration
    elif case == "dirty":
        _commit(integration, 2)
        (integration / SOURCE).write_text("VALUE = 999\n")
        run_tree = integration
    else:
        _commit(integration, 2)
        _git(close, "switch", "-c", "unlanded-descendant")
        _commit(close, 3)
        run_tree = close
    gate = _gate(close, run_tree, candidate)
    assert gate.status == "failed", gate.message
    assert TEST in gate.details["pytest_uncovered_paths"]
    assert reason in gate.message


def test_exact_candidate_bytes_keep_credit_after_docs_only_commit(
    repository: tuple[Path, Path, str],
) -> None:
    integration, close, candidate = repository
    _git(close, "switch", "-c", "docs-only")
    (close / "README.md").write_text("Documentation only\n")
    _git(close, "add", ".")
    _git(close, "commit", "-qm", "docs only")
    gate = _gate(integration, close, candidate)
    assert gate.status == "passed", gate.message
    assert gate.details["pytest_uncovered_paths"] == []


def test_landed_intermediate_commit_credits_detached_run_checkout(
    repository: tuple[Path, Path, str], tmp_path: Path
) -> None:
    integration, close, candidate = repository
    middle = _commit(integration, 2)
    _commit(integration, 3)
    run_tree = tmp_path / "landed-middle"
    _git(integration, "worktree", "add", "--detach", str(run_tree), middle)
    gate = _gate(close, run_tree, candidate)
    assert gate.status == "passed", gate.message
    assert gate.details["pytest_uncovered_paths"] == []


@pytest.mark.parametrize("kind", ["staged", "untracked", "unstaged"])
@pytest.mark.parametrize("same_root", [False, True])
def test_dirty_integration_never_earns_landed_tree_credit(
    repository: tuple[Path, Path, str], kind: str, same_root: bool
) -> None:
    integration, close, candidate = repository
    _commit(integration, 2)
    if kind == "untracked":
        (integration / "untracked.py").write_text("VALUE = 99\n")
    else:
        (integration / SOURCE).write_text("VALUE = 99\n")
        if kind == "staged":
            _git(integration, "add", SOURCE)
    gate = _gate(integration if same_root else close, integration, candidate)
    assert gate.status == "failed", gate.message
    assert TEST in gate.details["pytest_uncovered_paths"]
    assert "run checkout is dirty" in gate.message


def test_run_before_candidate_stays_uncredited(
    repository: tuple[Path, Path, str],
) -> None:
    integration, close, _old = repository
    candidate = _commit(integration, 2)
    gate = _gate(integration, close, candidate)
    assert gate.status == "failed", gate.message
    assert TEST in gate.details["pytest_uncovered_paths"]
    assert "run HEAD is not on" in gate.message


def test_off_task_author_copy_proof_stays_exact_candidate_bytes(
    repository: tuple[Path, Path, str],
) -> None:
    from gobby.tasks.close_test_coverage import CloseCandidate, candidate_pytest_paths

    integration, close, candidate = repository
    _commit(integration, 2)
    now = datetime.now(UTC)
    run = TranscriptValidationRun(
        session_id="other-session",
        source="test",
        command=f"uv run pytest {TEST}",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="success",
        started_at=now,
        completed_at=now,
        order=1,
        exit_code=0,
        output="1 passed in 0.01s",
        workdir=str(integration),
    )
    assert (
        candidate_pytest_paths(run, (TEST,), (TEST, SOURCE), CloseCandidate(str(close), candidate))
        == frozenset()
    )
