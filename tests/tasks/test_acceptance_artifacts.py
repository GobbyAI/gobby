"""Deterministic acceptance-artifact, provenance, and TDD gate tests."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gobby.tasks import acceptance_artifacts as artifacts_module
from gobby.tasks.acceptance_artifacts import (
    AcceptanceTest,
    evaluate_acceptance_artifacts,
    malformed_test_reference_findings,
    validate_structured_file_evidence,
    validation_run_covers_test,
    validation_run_names_test,
)
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence, is_test_convention_path
from gobby.tasks.transcript_evidence import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
    merge_transcript_evidence,
)
from gobby.tasks.transcript_outcomes import EvidenceOutcome

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_artifact_references_ignore_prose_file_line_token() -> None:
    criteria = "The plan uses file:line anchors.\nfile: `docs/evidence.md`."

    assert artifacts_module.extract_artifact_references(criteria, "file") == ("docs/evidence.md",)


def test_artifact_references_ignore_bare_prose_words() -> None:
    criteria = (
        "3) Dispatch test: an openapi-template server registers. "
        "The file: config is regenerated. Test: select the newest row."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == ()
    assert artifacts_module.extract_artifact_references(criteria, "file") == ()


def test_artifact_references_keep_pathlike_bare_tokens() -> None:
    criteria = (
        "test: tests/tasks/test_validation.py::test_gate.\n"
        "file: docs/evidence.md,\n"
        "file: .gobby/plans/x.yaml."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/tasks/test_validation.py::test_gate",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == (
        "docs/evidence.md",
        ".gobby/plans/x.yaml",
    )


def test_backticked_references_bypass_the_bare_token_shape_filter() -> None:
    criteria = "test: `oops`\nfile: `notes`"

    assert artifacts_module.extract_artifact_references(criteria, "test") == ("oops",)
    assert artifacts_module.extract_artifact_references(criteria, "file") == ("notes",)


def test_line_leading_references_support_markdown_prefixes() -> None:
    criteria = (
        "test: `tests/test_plain.py::test_plain`\n"
        "- file: docs/bullet.md\n"
        "2) test: tests/test_numbered.py::test_numbered\n"
        "**test**: `tests/test_bold.py::test_bold`\n"
        "**file**: `docs/bold.md`\n"
        "> test: `tests/test_quote.py::test_quote`"
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/test_plain.py::test_plain",
        "tests/test_numbered.py::test_numbered",
        "tests/test_bold.py::test_bold",
        "tests/test_quote.py::test_quote",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == (
        "docs/bullet.md",
        "docs/bold.md",
    )


def test_inline_code_examples_are_not_artifact_references() -> None:
    criteria = (
        "The schema describes `test: path::test_symbol`, examples use "
        "`test: tests/x.py::test_y`, and docs mention `file: docs/foo.md`.\n"
        "- test: `tests/a/test_b.py::test_c`"
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/a/test_b.py::test_c",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == ()


def test_bare_reference_never_keeps_trailing_backtick() -> None:
    criteria = "test: tests/test_feature.py::test_feature`\nfile: docs/evidence.md`"

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/test_feature.py::test_feature",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == ("docs/evidence.md",)


def test_backticked_test_reference_without_symbol_still_fails(tmp_path: Path) -> None:
    result = evaluate_acceptance_artifacts(
        criteria="test: `tests/tasks/test_validation.py`.",
        repo_path=str(tmp_path),
        commit_shas=[],
    )

    assert result.findings
    assert "malformed test reference" in result.findings[0]


def test_malformed_test_reference_findings_reports_every_invalid_reference() -> None:
    findings = malformed_test_reference_findings(
        """\
test: `tests/x/test_y.py`
test: `oops`
test: tests/z/test_w.py
test: `tests/x/test_y.py::test_valid`
3) Dispatch test: an openapi-template server registers.
"""
    )

    assert findings == (
        "tests/x/test_y.py: malformed test reference; expected path::test_symbol",
        "oops: malformed test reference; expected path::test_symbol",
        "tests/z/test_w.py: malformed test reference; expected path::test_symbol",
    )


def test_deliberate_missing_file_reference_keeps_actionable_diagnostic(tmp_path: Path) -> None:
    result = evaluate_acceptance_artifacts(
        criteria="file: `docs/missing.md`.",
        repo_path=str(tmp_path),
        commit_shas=[],
    )

    assert result.evidence_files == ("docs/missing.md",)
    assert result.findings == (
        "docs/missing.md: referenced evidence file is missing or unreadable",
    )


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


def test_rust_validation_runs_match_unit_test_module_path() -> None:
    symbol = "project_name_lookup_deduplicates_repeated_local_checkout"
    test = AcceptanceTest(
        reference=f"crates/gcode/src/config/tests.rs::{symbol}",
        path="crates/gcode/src/config/tests.rs",
        symbol=symbol,
        body=f"fn {symbol}() {{}}",
    )

    assert validation_run_covers_test(
        "cargo nextest run -p gobby-code",
        f"    PASS [0.004s] gobby-code config::tests::{symbol}",
        test,
    )
    assert validation_run_names_test(
        "cargo nextest run -p gobby-code",
        f"    FAIL [0.004s] gobby-code config::tests::{symbol}\npanicked at 'failed'",
        test,
    )


def test_rust_validation_runs_match_inline_tests_module() -> None:
    test = AcceptanceTest(
        reference="crates/gcode/src/cli_error.rs::reports_error",
        path="crates/gcode/src/cli_error.rs",
        symbol="reports_error",
        body="fn reports_error() {}",
    )

    assert validation_run_covers_test(
        "cargo test -p gobby-code reports_error",
        "test cli_error::tests::reports_error ... ok",
        test,
    )
    assert validation_run_names_test(
        "cargo test -p gobby-code reports_error",
        "test cli_error::tests::reports_error ... FAILED\npanicked at 'failed'",
        test,
    )


def test_rust_validation_runs_match_integration_test_stem() -> None:
    test = AcceptanceTest(
        reference="crates/gterminal/tests/frame_protocol.rs::frame_round_trips",
        path="crates/gterminal/tests/frame_protocol.rs",
        symbol="frame_round_trips",
        body="fn frame_round_trips() {}",
    )

    assert validation_run_covers_test(
        "cargo nextest run -p gobby-terminal",
        "    PASS [0.004s] gobby-terminal::frame_protocol frame_round_trips",
        test,
    )
    assert validation_run_names_test(
        "cargo nextest run -p gobby-terminal",
        "    FAIL [0.004s] gobby-terminal::frame_protocol frame_round_trips\npanicked at 'failed'",
        test,
    )


def test_rust_validation_runs_reject_different_symbol() -> None:
    test = AcceptanceTest(
        reference="crates/gcode/src/config/tests.rs::expected_symbol",
        path="crates/gcode/src/config/tests.rs",
        symbol="expected_symbol",
        body="fn expected_symbol() {}",
    )
    output = "    PASS [0.004s] gobby-code config::tests::different_symbol"

    assert not validation_run_covers_test("cargo nextest run -p gobby-code", output, test)
    assert not validation_run_names_test("cargo nextest run -p gobby-code", output, test)


def test_rust_validation_runs_reject_inconsistent_module_prefix() -> None:
    test = AcceptanceTest(
        reference="crates/gcode/src/config/tests.rs::expected_symbol",
        path="crates/gcode/src/config/tests.rs",
        symbol="expected_symbol",
        body="fn expected_symbol() {}",
    )
    output = "    FAIL [0.004s] gobby-code other_mod::tests::expected_symbol"

    assert not validation_run_covers_test("cargo nextest run -p gobby-code", output, test)
    assert not validation_run_names_test("cargo nextest run -p gobby-code", output, test)


def test_rust_validation_runs_require_symbol_word_boundary() -> None:
    test = AcceptanceTest(
        reference="crates/gcode/src/config/tests.rs::expected_symbol",
        path="crates/gcode/src/config/tests.rs",
        symbol="expected_symbol",
        body="fn expected_symbol() {}",
    )
    output = "    PASS [0.004s] gobby-code config::tests::expected_symbol_extended"

    assert not validation_run_covers_test("cargo nextest run -p gobby-code", output, test)
    assert not validation_run_names_test("cargo nextest run -p gobby-code", output, test)


def test_test_bodies_pinned_to_linked_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    test_file = repo / "tests" / "test_feature.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "def test_feature() -> None:\n    assert feature() == 'committed'\n",
        encoding="utf-8",
    )
    linked_sha = _commit(repo, "committed acceptance test")
    test_file.write_text(
        "def test_feature() -> None:\n    assert feature() == 'working-tree'\n",
        encoding="utf-8",
    )

    result = evaluate_acceptance_artifacts(
        criteria="Feature works.\ntest: tests/test_feature.py::test_feature",
        repo_path=str(repo),
        commit_shas=[linked_sha],
    )

    assert result.passed is True
    assert len(result.tests) == 1
    assert "'committed'" in result.tests[0].body
    assert "'working-tree'" not in result.tests[0].body


def test_rust_test_body_is_extracted_from_linked_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    test_file = repo / "crates" / "example" / "tests" / "feature.rs"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        '#[test]\nfn feature_works() {\n    let value = "committed";\n'
        '    assert_eq!(value, "committed");\n}\n',
        encoding="utf-8",
    )
    linked_sha = _commit(repo, "committed Rust acceptance test")
    test_file.write_text(
        '#[test]\nfn feature_works() {\n    let value = "working-tree";\n'
        '    assert_eq!(value, "working-tree");\n}\n',
        encoding="utf-8",
    )

    result = evaluate_acceptance_artifacts(
        criteria="Feature works.\ntest: crates/example/tests/feature.rs::feature_works",
        repo_path=str(repo),
        commit_shas=[linked_sha],
    )

    assert result.passed is True
    assert len(result.tests) == 1
    assert '"committed"' in result.tests[0].body
    assert '"working-tree"' not in result.tests[0].body


def test_python_placebo_acceptance_test_is_named(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = """
def test_feature() -> None:
    test_other()
    assert value or True
"""
    monkeypatch.setattr(artifacts_module, "_resolve_test_body", AsyncMock(return_value=body))

    result = evaluate_acceptance_artifacts(
        criteria="Feature works.\ntest: tests/test_feature.py::test_feature",
        repo_path=str(tmp_path),
        commit_shas=["linked"],
    )

    assert result.passed is False
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
    monkeypatch.setattr(artifacts_module, "_resolve_test_body", AsyncMock(return_value=body))

    result = evaluate_acceptance_artifacts(
        criteria=(
            "Frames round trip.\n"
            "test: crates/gterminal/tests/frame_protocol.rs::protocol_frame_roundtrip"
        ),
        repo_path=str(tmp_path),
        commit_shas=["linked"],
    )

    assert result.passed is False
    assert result.findings == (
        "crates/gterminal/tests/frame_protocol.rs::protocol_frame_roundtrip: "
        "contains a constant, stub, or placebo assertion",
    )


@pytest.mark.parametrize(
    ("path", "symbol", "body"),
    [
        (
            "tests/test_schema.py",
            "test_schema_contract",
            """
def test_schema_contract() -> None:
    database = test_database()
    assert database is not None
""",
        ),
        (
            "crates/gcore/tests/schema_contract.rs",
            "schema_contract",
            """
#[test]
fn schema_contract() -> anyhow::Result<()> {
    let Some(database) = test_database()? else {
        return Ok(());
    };
    assert!(database.is_ready());
    Ok(())
}
""",
        ),
    ],
)
def test_test_named_helper_call_is_not_delegation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
    symbol: str,
    body: str,
) -> None:
    monkeypatch.setattr(artifacts_module, "_resolve_test_body", AsyncMock(return_value=body))

    result = evaluate_acceptance_artifacts(
        criteria=f"Contract is executable.\ntest: {path}::{symbol}",
        repo_path=str(tmp_path),
        commit_shas=["linked"],
    )

    assert result.passed is True
    assert result.findings == ()


@pytest.mark.parametrize(
    ("path", "symbol", "body", "finding"),
    [
        (
            "tests/test_schema.py",
            "test_schema_contract",
            """
def test_schema_contract() -> None:
    test_other_contract()
""",
            "contains no executable assertion",
        ),
        (
            "crates/gcore/tests/schema_contract.rs",
            "schema_contract",
            """
#[test]
fn schema_contract() {
    test_other_contract();
}
""",
            "contains no executable assertion or panic expectation",
        ),
    ],
)
def test_delegation_only_body_still_requires_an_executable_assertion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
    symbol: str,
    body: str,
    finding: str,
) -> None:
    monkeypatch.setattr(artifacts_module, "_resolve_test_body", AsyncMock(return_value=body))

    result = evaluate_acceptance_artifacts(
        criteria=f"Contract is executable.\ntest: {path}::{symbol}",
        repo_path=str(tmp_path),
        commit_shas=["linked"],
    )

    assert result.passed is False
    assert result.findings == (f"{path}::{symbol}: {finding}",)


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


def test_tdd_evidence_accepts_one_cycle_with_multiple_green_artifacts() -> None:
    started = datetime(2026, 8, 30, tzinfo=UTC)
    driving = AcceptanceTest(
        reference="tests/test_feature.py::test_driving_behavior",
        path="tests/test_feature.py",
        symbol="test_driving_behavior",
        body="def test_driving_behavior(): assert feature() == 1",
    )
    supporting = AcceptanceTest(
        reference="tests/test_support.py::test_supporting_behavior",
        path="tests/test_support.py",
        symbol="test_supporting_behavior",
        body="def test_supporting_behavior(): assert feature() != 2",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit("tests/test_feature.py", started, 1),
            _edit("tests/test_support.py", started + timedelta(seconds=30), 2),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            _run(
                driving,
                started + timedelta(minutes=1),
                "failure",
                "FAILED tests/test_feature.py::test_driving_behavior\nE assert 0 == 1",
                2,
            ),
            _run(
                driving,
                started + timedelta(minutes=3),
                "success",
                "tests/test_feature.py::test_driving_behavior PASSED",
                4,
            ),
            _run(
                supporting,
                started + timedelta(minutes=4),
                "success",
                "tests/test_support.py::test_supporting_behavior PASSED",
                5,
            ),
        ),
    )

    result = evaluate_tdd_evidence((driving, supporting), evidence)

    assert result.passed is True
    assert result.red_runs == (f"pytest {driving.reference}",)
    assert result.green_runs == (
        f"pytest {driving.reference}",
        f"pytest {supporting.reference}",
    )

    missing_supporting_green = TranscriptEvidence(
        edits=evidence.edits,
        validation_runs=evidence.validation_runs[:-1],
    )
    incomplete = evaluate_tdd_evidence(
        (driving, supporting),
        missing_supporting_green,
    )
    assert incomplete.passed is False
    assert any(supporting.reference in finding for finding in incomplete.findings)


def test_tdd_evidence_rejects_non_test_green_run() -> None:
    started = datetime(2026, 8, 30, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    lint_green = replace(
        _run(test, started + timedelta(minutes=3), "success", "All checks passed!", 4),
        command="ruff check tests/test_feature.py",
        categories=("lint",),
        matcher_id="ruff",
        label="ruff",
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
            lint_green,
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is False
    assert result.green_runs == ()


def test_tdd_evidence_rejects_test_quality_audit_as_green() -> None:
    started = datetime(2026, 8, 30, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    audit_green = replace(
        _run(test, started + timedelta(minutes=3), "success", "Issues: 0", 4),
        command="gobby test-quality audit tests/test_feature.py --fail-on-new",
        categories=("test",),
        matcher_id="gobby-test-quality-audit",
        label="Gobby test-quality audit",
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
            audit_green,
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is False
    assert result.green_runs == ()


def test_tdd_evidence_does_not_borrow_assertion_from_other_test() -> None:
    started = datetime(2026, 8, 30, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    broad_output = """\
tests/test_feature.py::test_feature PASSED
tests/test_other.py::test_other FAILED
E   AssertionError: assert 0 == 1
"""
    evidence = TranscriptEvidence(
        edits=(
            _edit("tests/test_feature.py", started, 1),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            _run(test, started + timedelta(minutes=1), "failure", broad_output, 2),
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
    assert result.red_runs == ()

    named_failure = TranscriptEvidence(
        edits=evidence.edits,
        validation_runs=(
            _run(
                test,
                started + timedelta(minutes=1),
                "failure",
                "tests/test_feature.py::test_feature FAILED\nE assert 0 == 1",
                2,
            ),
            evidence.validation_runs[1],
        ),
    )
    assert evaluate_tdd_evidence((test,), named_failure).passed is True


def test_tdd_evidence_accepts_rtk_class_dot_failure() -> None:
    started = datetime(2026, 8, 30, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::TestFeature::test_behavior",
        path="tests/test_feature.py",
        symbol="TestFeature::test_behavior",
        body="def test_behavior(): assert feature() == 1",
    )
    edits = (
        _edit("tests/test_feature.py", started, 1),
        _edit("src/feature.py", started + timedelta(minutes=2), 3),
    )
    green = _run(
        test,
        started + timedelta(minutes=3),
        "success",
        "tests/test_feature.py::TestFeature::test_behavior PASSED",
        4,
    )
    rtk_failure = """\
1. [FAIL] TestFeature.test_behavior
    raise AssertionError(msg)
    E   AssertionError: assert 0 == 1
"""

    accepted = evaluate_tdd_evidence(
        (test,),
        TranscriptEvidence(
            edits=edits,
            validation_runs=(
                _run(test, started + timedelta(minutes=1), "failure", rtk_failure, 2),
                green,
            ),
        ),
    )

    assert accepted.passed is True
    assert accepted.red_runs

    non_assertion = evaluate_tdd_evidence(
        (test,),
        TranscriptEvidence(
            edits=edits,
            validation_runs=(
                _run(
                    test,
                    started + timedelta(minutes=1),
                    "failure",
                    "1. [FAIL] TestFeature.test_behavior\nE RuntimeError: broken fixture",
                    2,
                ),
                green,
            ),
        ),
    )
    assert non_assertion.passed is False
    assert non_assertion.red_runs == ()


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


def test_tdd_evidence_accepts_did_not_raise_red() -> None:
    started = datetime(2026, 8, 31, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): raises(ReportError, feature)",
    )
    red_output = """\
______________________________ test_feature ______________________________
tests/test_feature.py:9: Failed
E   Failed: DID NOT RAISE <class 'ReportError'>
=========================== short test summary info ============================
FAILED tests/test_feature.py::test_feature - Failed: DID NOT RAISE ReportError
"""
    evidence = TranscriptEvidence(
        edits=(
            _edit(test.path, started, 1),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            _run(test, started + timedelta(minutes=1), "failure", red_output, 2),
            _run(test, started + timedelta(minutes=3), "success", "1 passed", 4),
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is True
    assert result.red_runs


@pytest.mark.parametrize(
    "traceback_line",
    (
        "tests/test_feature.py:9: in test_feature",
        "tests/test_feature.py:9: NotImplementedError",
    ),
)
def test_tdd_evidence_accepts_not_implemented_red(traceback_line: str) -> None:
    started = datetime(2026, 8, 31, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    red_output = f"""\
______________________________ test_feature ______________________________
{traceback_line}
E   NotImplementedError
=========================== short test summary info ============================
FAILED tests/test_feature.py::test_feature - NotImplementedError
"""
    evidence = TranscriptEvidence(
        edits=(
            _edit(test.path, started, 1),
            _edit("src/feature.py", started + timedelta(seconds=30), 2),
            _edit("src/feature.py", started + timedelta(minutes=2), 4),
        ),
        validation_runs=(
            _run(test, started + timedelta(minutes=1), "failure", red_output, 3),
            _run(test, started + timedelta(minutes=3), "success", "1 passed", 5),
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is True
    assert result.red_runs
    assert result.green_runs


def test_tdd_evidence_accepts_pytest_q_red_with_trailing_failed_line() -> None:
    started = datetime(2026, 8, 31, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    red_output = """\
______________________________ test_feature ______________________________
tests/test_feature.py:9: in test_feature
E   AssertionError: assert 0 == 1
=========================== short test summary info ============================
FAILED tests/test_feature.py::test_feature - AssertionError: assert 0 == 1
"""
    evidence = TranscriptEvidence(
        edits=(
            _edit(test.path, started, 1),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            _run(test, started + timedelta(minutes=1), "failure", red_output, 2),
            _run(test, started + timedelta(minutes=3), "success", "1 passed", 4),
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is True
    assert result.red_runs


def test_green_run_names_symbol_covers_test() -> None:
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )

    assert validation_run_covers_test("uv run pytest -q -k test_feature", "1 passed", test)
    assert validation_run_covers_test("uv run pytest -q", "test_feature.py\n1 passed", test)
    assert not validation_run_covers_test(
        "uv run pytest -q -k test_feature_extra", "1 passed", test
    )
    assert not validation_run_covers_test("uv run pytest -q", "4 passed", test)


def test_validation_matchers_compare_credited_core_command() -> None:
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    prefixed = TranscriptValidationRun(
        session_id="session",
        source="codex",
        command="cd /repo && GOBBY_TEST_PROTECT=1 pytest tests/test_feature.py::test_feature",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="success",
        started_at=datetime(2026, 9, 4, tzinfo=UTC),
        completed_at=datetime(2026, 9, 4, tzinfo=UTC),
        order=1,
    )
    wrapped = TranscriptValidationRun(
        session_id="session",
        source="codex",
        command="pytest tests/test_feature.py::test_feature | tail -1",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="success",
        started_at=datetime(2026, 9, 4, tzinfo=UTC),
        completed_at=datetime(2026, 9, 4, tzinfo=UTC),
        order=2,
    )

    assert validation_run_names_test(prefixed.core_command, prefixed.output, test)
    assert validation_run_covers_test(prefixed.core_command, prefixed.output, test)
    assert not validation_run_names_test(wrapped.core_command, wrapped.command, test)
    assert not validation_run_covers_test(wrapped.core_command, wrapped.command, test)


def test_tdd_evidence_rejection_names_run_and_unattributed_symbol() -> None:
    started = datetime(2026, 8, 31, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::TestFeature::test_target",
        path="tests/test_feature.py",
        symbol="TestFeature::test_target",
        body="def test_target(): assert feature() == 1",
    )
    red_output = """\
________________________ TestOther.test_sibling _________________________
tests/test_feature.py:20: in test_sibling
E   AssertionError: assert 0 == 1
"""
    evidence = TranscriptEvidence(
        edits=(
            _edit(test.path, started, 1),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(_run(test, started + timedelta(minutes=1), "failure", red_output, 2),),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is False
    assert "pytest tests/test_feature.py::TestFeature::test_target" in result.findings[0]
    assert "no attributable failure section for 'TestFeature::test_target'" in result.findings[0]


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


@pytest.mark.parametrize(
    ("path", "convention"),
    [
        ("tests/tasks/test_acceptance_artifacts.py", True),
        ("src/gobby/tasks/guards_test.py", True),
        ("web/src/login.test.tsx", True),
        ("web/src/Login.spec.ts", True),
        ("pkg/store_test.go", True),
        ("tests/skills/scenarios/plan-mechanic/bounded-repair.yaml", True),
        ("tests/conftest.py", True),
        ("tests/skills/scenario_runner.py", True),
        ("crates/gcore/tests/schema_contract.rs", True),
        ("src/gobby/tasks/tdd_evidence.py", False),
    ],
)
def test_test_convention_paths_cover_every_language_and_test_tree(
    path: str, convention: bool
) -> None:
    assert is_test_convention_path(path) is convention
