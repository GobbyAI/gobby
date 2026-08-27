"""Deterministic acceptance-artifact, provenance, and TDD gate tests."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.tasks import acceptance_artifacts as artifacts_module
from gobby.tasks.acceptance_artifacts import (
    AcceptanceTest,
    evaluate_acceptance_artifacts,
    validate_structured_file_evidence,
    validation_run_names_test,
)
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.transcript_evidence import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
    merge_transcript_evidence,
)
from gobby.tasks.transcript_outcomes import EvidenceOutcome

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_transcript_evidence_imports_in_fresh_interpreter() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import gobby.tasks.transcript_evidence; "
                "assert 'gobby.mcp_proxy.server' not in sys.modules, "
                "'importing transcript evidence loaded the MCP server'; "
                "from gobby.mcp_proxy import MCPClientManager, create_mcp_server"
            ),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_validation_run_names_class_qualified_pytest_node_id() -> None:
    test = AcceptanceTest(
        reference="tests/test_feature.py::TestFeature.test_feature",
        path="tests/test_feature.py",
        symbol="TestFeature.test_feature",
        body="def test_feature(): assert feature()",
    )

    assert validation_run_names_test(
        "pytest tests/test_feature.py::TestFeature::test_feature",
        "E AssertionError: failed",
        test,
    )
    assert not validation_run_names_test(
        "pytest tests/test_other.py::TestFeature::test_feature",
        "E AssertionError: failed",
        test,
    )


def test_test_body_resolution_requests_gcode_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run_command(command: list[str], _repo_path: str) -> str:
        calls.append(command)
        if command[1] == "search-symbol":
            return (
                '{"results":[{"id":"symbol-id","file_path":"tests/test_feature.py",'
                '"name":"test_feature","qualified_name":"test_feature"}]}'
            )
        return '{"source":"def test_feature(): pass"}'

    monkeypatch.setattr(artifacts_module, "_run_command", run_command)

    body = artifacts_module._resolve_test_body("tests/test_feature.py", "test_feature", "/repo")

    assert body == "def test_feature(): pass"
    assert calls[0][-4:] == ["--format", "json", "--limit", "20"]
    assert calls[1] == ["gcode", "symbol", "symbol-id", "--format", "json"]


def test_python_placebo_acceptance_test_is_named(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = """
def test_feature() -> None:
    test_other()
    assert value or True
"""
    monkeypatch.setattr(artifacts_module, "_resolve_test_body", lambda *_args: body)

    result = evaluate_acceptance_artifacts(
        criteria="Feature works. test: tests/test_feature.py::test_feature",
        repo_path=str(tmp_path),
        commit_shas=[],
    )

    assert result.passed is False
    assert any("delegates acceptance" in finding for finding in result.findings)
    assert any("tautological assertion" in finding for finding in result.findings)
    assert all("test_feature" in finding for finding in result.findings)


def test_rust_format_constant_placebo_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = """
#[test]
fn protocol_frame_roundtrip() {
    assert_eq!(format!("{}", 256), "256");
}
"""
    monkeypatch.setattr(artifacts_module, "_resolve_test_body", lambda *_args: body)

    result = evaluate_acceptance_artifacts(
        criteria=(
            "Frames round trip. "
            "test: crates/gterminal/tests/frame_protocol.rs::protocol_frame_roundtrip"
        ),
        repo_path=str(tmp_path),
        commit_shas=[],
    )

    assert result.passed is False
    assert result.findings == (
        "crates/gterminal/tests/frame_protocol.rs::protocol_frame_roundtrip: "
        "contains a constant, stub, or placebo assertion",
    )


def test_structured_evidence_rejects_postdated_sha_and_missing_workflow(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    Path(repo, "seed.txt").write_text("seed\n", encoding="utf-8")
    cited_sha = _commit(repo, "seed")
    evidence = f"""# Evidence

## Run
- workflow_name: Weekly Producer
- run_url: https://github.com/GobbyAI/gobby/actions/runs/123
- commit_sha: {cited_sha}
- utc_timestamp: 2000-01-01T00:00:00Z
"""
    path = Path(repo, "docs", "evidence.md")
    path.parent.mkdir()
    path.write_text(evidence, encoding="utf-8")
    evidence_sha = _commit(repo, "evidence")

    findings = validate_structured_file_evidence(
        evidence_files=("docs/evidence.md",),
        repo_path=str(repo),
        commit_shas=[evidence_sha],
    )

    assert any("newer than the cited run timestamp" in finding for finding in findings)
    assert any("producer workflow 'Weekly Producer' is absent" in finding for finding in findings)
    assert len(findings) == 2


def test_structured_evidence_accepts_locally_provable_run(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    workflow = Path(repo, ".github", "workflows", "weekly.yml")
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: Weekly Producer\non: workflow_dispatch\n", encoding="utf-8")
    cited_sha = _commit(repo, "producer")
    path = Path(repo, "docs", "evidence.md")
    path.parent.mkdir()
    path.write_text(
        f"""## Run
- workflow_name: Weekly Producer
- run_url: https://github.com/GobbyAI/gobby/actions/runs/123
- commit_sha: {cited_sha}
- utc_timestamp: 2099-01-01T00:00:00Z
""",
        encoding="utf-8",
    )
    evidence_sha = _commit(repo, "evidence")

    findings = validate_structured_file_evidence(
        evidence_files=("docs/evidence.md",),
        repo_path=str(repo),
        commit_shas=[evidence_sha],
    )

    assert findings == ()


def test_native_backend_evidence_regression_fails_on_local_contradictions() -> None:
    findings = validate_structured_file_evidence(
        evidence_files=("docs/evidence/native-backend-flip.md",),
        repo_path=str(REPO_ROOT),
        commit_shas=["d07111cf2d", "6b4e032125"],
    )

    assert any("89f7b404" in finding and "newer" in finding for finding in findings)
    assert any("c62e4bae" in finding and "newer" in finding for finding in findings)
    assert any("89f7b404" in finding and "producer workflow" in finding for finding in findings)


def test_tdd_evidence_requires_assertion_red_before_source_edit() -> None:
    started = datetime(2026, 8, 21, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit("tests/test_feature.py", started, 1),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            _run(
                test,
                started + timedelta(minutes=1),
                "failure",
                "FAILED tests/test_feature.py::test_feature\nE assert 0 == 1",
                2,
            ),
            _run(
                test,
                started + timedelta(minutes=3),
                "success",
                "tests/test_feature.py::test_feature PASSED",
                4,
            ),
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is True
    assert result.red_runs
    assert result.green_runs


def test_tdd_evidence_ignores_other_test_edits_before_red() -> None:
    started = datetime(2026, 8, 21, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit("tests/test_feature.py", started, 1),
            _edit("tests/test_support.py", started, 2),
            _edit("src/feature.py", started + timedelta(minutes=2), 4),
        ),
        validation_runs=(
            _run(
                test,
                started + timedelta(minutes=1),
                "failure",
                "FAILED tests/test_feature.py::test_feature\nE assert 0 == 1",
                3,
            ),
            _run(
                test,
                started + timedelta(minutes=3),
                "success",
                "tests/test_feature.py::test_feature PASSED",
                5,
            ),
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is True
    assert result.red_runs
    assert result.green_runs


def test_tdd_evidence_accepts_later_repair_cycle() -> None:
    started = datetime(2026, 8, 21, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    repaired = TranscriptEvidence(
        edits=(
            _edit("tests/test_feature.py", started + timedelta(hours=6), 1),
            _edit("src/feature.py", started + timedelta(hours=6, minutes=1), 2),
            _edit("tests/test_feature.py", started + timedelta(hours=6, minutes=2), 3),
            _edit("src/feature.py", started + timedelta(hours=6, minutes=4), 5),
        ),
        validation_runs=(
            _run(
                test,
                started + timedelta(minutes=3),
                "failure",
                "FAILED tests/test_feature.py::test_feature\nE assert 0 == 1",
                4,
            ),
            _run(
                test,
                started + timedelta(minutes=5),
                "success",
                "tests/test_feature.py::test_feature PASSED",
                6,
            ),
        ),
    )

    result = evaluate_tdd_evidence((test,), repaired)

    assert result.passed is True
    assert result.red_runs
    assert result.green_runs

    post_implementation_red = TranscriptEvidence(
        edits=repaired.edits[:2],
        validation_runs=repaired.validation_runs,
    )
    assert evaluate_tdd_evidence((test,), post_implementation_red).passed is False

    test_only_green = TranscriptEvidence(
        edits=(
            _edit("tests/test_feature.py", started, 1),
            _edit("tests/test_feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            _run(
                test,
                started + timedelta(minutes=1),
                "failure",
                "FAILED tests/test_feature.py::test_feature\nE assert 0 == 1",
                2,
            ),
            _run(
                test,
                started + timedelta(minutes=3),
                "success",
                "tests/test_feature.py::test_feature PASSED",
                4,
            ),
        ),
    )
    assert evaluate_tdd_evidence((test,), test_only_green).passed is False


def test_tdd_evidence_merges_handoff_sessions_by_position() -> None:
    """Owner red and closer green form one cycle once merged order is global."""
    started = datetime(2026, 8, 21, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    red_output = "FAILED tests/test_feature.py::test_feature\nE assert 0 == 1"
    owner = TranscriptEvidence(
        edits=(_edit("tests/test_feature.py", started, 300, session_id="owner"),),
        validation_runs=(
            _run(test, started + timedelta(minutes=1), "failure", red_output, 305, "owner"),
        ),
    )
    closer = TranscriptEvidence(
        edits=(_edit("src/feature.py", started + timedelta(minutes=2), 10, session_id="closer"),),
        validation_runs=(
            _run(
                test,
                started + timedelta(minutes=3),
                "success",
                "tests/test_feature.py::test_feature PASSED",
                15,
                "closer",
            ),
        ),
    )

    assert evaluate_tdd_evidence((test,), merge_transcript_evidence(owner, closer)).passed is True

    stale_closer = TranscriptEvidence(
        validation_runs=(
            _run(
                test,
                started - timedelta(hours=1),
                "success",
                "tests/test_feature.py::test_feature PASSED",
                150,
                "closer",
            ),
        ),
    )
    owner_without_green = TranscriptEvidence(
        edits=(
            _edit("tests/test_feature.py", started, 3, session_id="owner"),
            _edit("src/feature.py", started + timedelta(minutes=2), 8, session_id="owner"),
        ),
        validation_runs=(
            _run(test, started + timedelta(minutes=1), "failure", red_output, 5, "owner"),
        ),
    )
    stale = evaluate_tdd_evidence(
        (test,), merge_transcript_evidence(owner_without_green, stale_closer)
    )

    assert stale.passed is False
    assert stale.green_runs == ()


@pytest.mark.parametrize(
    "path",
    ["docs/plans/feature.md", ".gobby/test-types-baseline.json", "AGENTS.md", "crates/CLAUDE.md"],
)
def test_tdd_evidence_rejects_documentation_as_production_edit(path: str) -> None:
    started = datetime(2026, 8, 21, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit("tests/test_feature.py", started, 1),
            _edit(path, started + timedelta(minutes=2), 3),
            _edit("tests/test_feature.py", started + timedelta(minutes=3), 4),
        ),
        validation_runs=(
            _run(
                test,
                started + timedelta(minutes=1),
                "failure",
                "FAILED tests/test_feature.py::test_feature\nE assert 0 == 1",
                2,
            ),
            _run(
                test,
                started + timedelta(minutes=4),
                "success",
                "tests/test_feature.py::test_feature PASSED",
                5,
            ),
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is False
    assert result.findings == (
        "tests/test_feature.py::test_feature: no production edit follows the test edit",
    )


def test_collection_import_error_is_missing_red_evidence() -> None:
    started = datetime(2026, 8, 21, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit("tests/test_feature.py", started, 1),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            _run(
                test,
                started + timedelta(minutes=1),
                "failure",
                "ImportError while importing tests/test_feature.py\n"
                "ERROR collecting tests/test_feature.py",
                2,
            ),
            _run(
                test,
                started + timedelta(minutes=3),
                "success",
                "tests/test_feature.py::test_feature PASSED",
                4,
            ),
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is False
    assert "missing assertion or panic failure" in result.findings[0]


def _edit(
    path: str, timestamp: datetime, order: int, session_id: str = "session"
) -> TranscriptEdit:
    return TranscriptEdit(
        session_id=session_id,
        source="codex",
        path=path,
        timestamp=timestamp,
        order=order,
        tool_name="apply_patch",
    )


def _run(
    test: AcceptanceTest,
    timestamp: datetime,
    outcome: EvidenceOutcome,
    output: str,
    order: int,
    session_id: str = "session",
) -> TranscriptValidationRun:
    return TranscriptValidationRun(
        session_id=session_id,
        source="codex",
        command=f"pytest {test.reference}",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome=outcome,
        started_at=timestamp,
        completed_at=timestamp + timedelta(seconds=1),
        order=order,
        exit_code=0 if outcome == "success" else 1,
        output=output,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Tests")
    _git(repo, "remote", "add", "origin", "https://github.com/GobbyAI/gobby.git")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout
