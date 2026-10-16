"""Collection credit is restricted to declared operator admission guards."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.tasks.close_checklist import CloseGateResult, evaluate_validation_commands
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun

TEST = "tests/e2e/test_live.py"
GUARDED = """import os
import pytest
_GRANT = "OPERATOR_AUTHORIZED"
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(
    os.environ.get("GOBBY_LIVE_EXECUTION") != _GRANT,
    reason="operator execution grant required",
)]
def test_live():
    assert True
"""


def _run(command: str, output: str, *, exit_code: int = 0) -> TranscriptValidationRun:
    now = datetime(2026, 10, 16, tzinfo=UTC)
    return TranscriptValidationRun(
        session_id="author",
        source="codex",
        command=command,
        categories=(),
        matcher_id="",
        label="command",
        outcome="success" if exit_code == 0 else "failure",
        started_at=now,
        completed_at=now,
        order=1,
        exit_code=exit_code,
        output=output,
    )


def _gate(
    root: Path,
    collection: TranscriptValidationRun,
    source: str = GUARDED,
    *,
    changed: tuple[str, ...] = (TEST,),
) -> CloseGateResult:
    path = root / TEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    real_test = replace(
        _run("uv run pytest tests/test_other.py -q", "1 passed in 0.01s"),
        categories=("test",),
        matcher_id="pytest",
        order=2,
    )
    audit = replace(
        _run(
            "uv run gobby test-types audit tests/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new",
            "Errors: 0",
        ),
        categories=("type_check",),
        matcher_id="gobby-test-types-audit",
        order=3,
    )
    return evaluate_validation_commands(
        task_category="code",
        has_attributed_edits=True,
        close_root=str(root),
        changed_paths=changed,
        evidence=TranscriptEvidence(validation_runs=(real_test, audit), command_runs=(collection,)),
    )


@pytest.mark.parametrize(
    ("launcher", "output"),
    [
        ("uv run pytest", "1 test collected in 0.01s"),
        ("uv run rtk pytest", "1 test collected in 0.01s"),
        ("rtk uv run pytest", "Pytest: 1 test collected in 0.01s"),
    ],
)
def test_guarded_collection_credits_changed_file(root: Path, launcher: str, output: str) -> None:
    gate = _gate(root, _run(f"{launcher} {TEST} --collect-only -q", output))
    assert gate.status == "passed", gate.message
    assert gate.details["pytest_uncovered_paths"] == []


@pytest.mark.parametrize(
    ("source", "output", "exit_code"),
    [
        ("def test_live():\n    assert True\n", "1 test collected in 0.01s", 0),
        (
            "pytestmark = pytest.mark.skipif(True, reason='operator')",
            "1 test collected in 0.01s",
            0,
        ),
        (GUARDED, "no tests collected in 0.01s", 0),
        (GUARDED, "collected 0 items", 0),
        (GUARDED, "1 test collected in 0.01s\n1 error", 1),
        (GUARDED, "1 skipped in 0.01s", 0),
        (GUARDED, "collected 1 item", 0),
    ],
)
def test_collection_without_guard_or_positive_completion_is_rejected(
    root: Path,
    source: str,
    output: str,
    exit_code: int,
) -> None:
    gate = _gate(
        root, _run(f"uv run pytest {TEST} --collect-only -q", output, exit_code=exit_code), source
    )
    assert gate.status == "failed"
    assert gate.details["pytest_uncovered_paths"] == [TEST]


@pytest.mark.parametrize("target", ["tests/", f"{TEST} tests/test_other.py", f"{TEST}::test_live"])
def test_collection_requires_one_complete_file(root: Path, target: str) -> None:
    gate = _gate(
        root, _run(f"uv run pytest {target} --collect-only -q", "1 test collected in 0.01s")
    )
    assert gate.details["pytest_uncovered_paths"] == [TEST]


def test_collection_never_credits_unguarded_sibling(root: Path) -> None:
    gate = _gate(
        root,
        _run(f"uv run pytest {TEST} --collect-only -q", "1 test collected in 0.01s"),
        changed=(TEST, "tests/test_ordinary.py"),
    )
    assert gate.details["pytest_uncovered_paths"] == ["tests/test_ordinary.py"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path
