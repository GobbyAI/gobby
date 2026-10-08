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
from gobby.tasks.tdd_evidence import (
    TddEvidenceResult,
    evaluate_tdd_evidence,
)
from gobby.tasks.tdd_paths import is_test_convention_path
from gobby.tasks.transcript_evidence import merge_transcript_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
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


def test_expansion_prefixed_references_are_extracted() -> None:
    criteria = (
        "Acceptance artifacts:\n"
        "- 2.3.1: test: `tests/test_expanded.py::test_expanded`\n"
        "- A.2b.3: test: `tests/test_alphanumeric.py::test_alphanumeric`\n"
        "- 2.3.2: file: `src/gobby/tasks/acceptance_artifacts.py`\n"
        "- 2: test: `tests/test_single_segment.py::test_ignored`\n"
        "Prose containing 2.3.3: test: `tests/test_ignored.py::test_ignored`.\n"
        "- Prose containing 2.3.4: file: `docs/ignored.md`."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/test_expanded.py::test_expanded",
        "tests/test_alphanumeric.py::test_alphanumeric",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == (
        "src/gobby/tasks/acceptance_artifacts.py",
    )


def test_inline_criterion_test_reference_is_extracted() -> None:
    criteria = (
        "3.2.2: A partition skips the write. test: "
        "`crates/x/src/a_tests.rs::unchanged_partition_skips_write`.\n"
        "- 3.2.3: The `test: examples/example.rs::not_a_test` sample is inert; "
        "test: `tests/test_real.py::test_real`.\n"
        "- test: `tests/test_list.py::test_list`\n"
        "1) 3.2.5: A numbered criterion. test: `tests/test_number.py::test_number`.\n"
        "Prose containing 3.2.4: test: `tests/test_prose.py::test_prose`."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "crates/x/src/a_tests.rs::unchanged_partition_skips_write",
        "tests/test_real.py::test_real",
        "tests/test_list.py::test_list",
        "tests/test_number.py::test_number",
    )


def test_multiple_inline_test_references_on_one_criterion_are_extracted() -> None:
    criteria = (
        "3.2.2: test: `tests/a.py::test_a` and test: `tests/b.py::test_b`; "
        "the `test: examples/example.py::not_a_test` example is inert."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/a.py::test_a",
        "tests/b.py::test_b",
    )


def test_numbered_criteria_extract_real_tests_after_quoted_example() -> None:
    criteria = (
        "7. Extract an inline test, such as '3.2.2: skips the write. test: "
        "`crates/x/src/a_tests.rs::unchanged_partition_skips_write`.' "
        "test: `tests/tasks/test_acceptance_artifacts.py::test_inline_criterion_test_reference_is_extracted`.\n"
        "8. A task with no named test fails. test: "
        "`tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_tdd_required_task_without_resolved_tests_fails_gate_12`.\n"
        "9. A task without TDD skips the gates. test: "
        "`tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_non_tdd_task_without_tests_still_skips_gates_11_and_12`."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/tasks/test_acceptance_artifacts.py::test_inline_criterion_test_reference_is_extracted",
        "tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_tdd_required_task_without_resolved_tests_fails_gate_12",
        "tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_non_tdd_task_without_tests_still_skips_gates_11_and_12",
    )


def test_numbered_criterion_keeps_a_quoted_real_test_reference() -> None:
    criteria = '7. Verify the named "test: `tests/x.py::test_x`" passes.'

    assert artifacts_module.extract_artifact_references(criteria, "test") == ("tests/x.py::test_x",)


def test_numbered_criterion_ignores_example_with_an_apostrophe() -> None:
    criteria = (
        "7. For example, 'it's a test: `tests/fake.py::test_fake`'; "
        "test: `tests/real.py::test_real`."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/real.py::test_real",
    )


@pytest.mark.parametrize("cue", ["For example", "Example", "e.g.", "such as"])
@pytest.mark.parametrize("separator", [",", ":"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_numbered_criterion_ignores_illustrative_quoted_test(
    cue: str, separator: str, quote: str
) -> None:
    criteria = (
        f"7. {cue}{separator} {quote}it's a test: `tests/fake.py::test_fake`{quote}; "
        "test: `tests/real.py::test_real`."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/real.py::test_real",
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


@pytest.mark.parametrize(
    "criteria",
    [
        "test: tests/x.py::test_x: a taskless reviewer is parked",
        "test: tests/x.py::test_x)",
        "1. A taskless reviewer is parked. test: tests/x.py::test_x: then reported",
        "1. A taskless reviewer is parked (test: tests/x.py::test_x)",
        "1. A taskless reviewer is parked (test: tests/x.py::test_x).",
    ],
)
def test_bare_reference_drops_trailing_colon_and_unbalanced_paren(criteria: str) -> None:
    assert artifacts_module.extract_artifact_references(criteria, "test") == ("tests/x.py::test_x",)


@pytest.mark.parametrize("suffix", ["", ":", ".", ").", "):"])
def test_bare_reference_keeps_parametrized_brackets(suffix: str) -> None:
    criteria = f"test: tests/x.py::test_x[a-(1)]{suffix}"

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/x.py::test_x[a-(1)]",
    )


def test_bare_reference_drops_sentence_ending_period() -> None:
    criteria = "1. The gate resolves test: tests/x.py::test_x."

    assert artifacts_module.extract_artifact_references(criteria, "test") == ("tests/x.py::test_x",)


def test_backticked_reference_keeps_trailing_colon() -> None:
    criteria = "test: `tests/x.py::test_x:`"

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/x.py::test_x:",
    )


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


def test_quoted_frontend_test_name_resolves_it_body() -> None:
    reference = (
        "web/src/components/settings/sections/__tests__/sections.coverage.test.ts"
        '::"renders the empty state"'
    )
    assert artifacts_module.parse_test_reference(reference) == (
        "web/src/components/settings/sections/__tests__/sections.coverage.test.ts",
        "renders the empty state",
    )
    assert (
        malformed_test_reference_findings(
            "test: `web/src/components/settings/sections/__tests__/"
            'sections.coverage.test.ts::"renders the empty state"`'
        )
        == ()
    )
    source = """
describe("settings", () => {
  it("renders the empty state", () => {
    expect(screen.getByRole("status")).toBeTruthy()
  })
  it('renders the filled state', () => {
    expect(screen.getByRole("status")).toBeFalsy()
  })
})
"""
    body = artifacts_module._extract_braced_test_body(
        source, "renders the empty state", "sections.test.ts"
    )
    assert 'it("renders the empty state"' in body
    assert "toBeTruthy()" in body
    assert "renders the filled state" not in body
    single = artifacts_module._extract_braced_test_body(
        source, "renders the filled state", "sections.test.ts"
    )
    assert "it('renders the filled state'" in single
    assert "toBeFalsy()" in single


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


def test_absolute_file_evidence_explains_repository_relative_requirement(tmp_path: Path) -> None:
    result = evaluate_acceptance_artifacts(
        criteria="file: `/tmp/external-report.md`.",
        repo_path=str(tmp_path),
        commit_shas=[],
    )

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert "repository-relative" in finding
    assert "committed evidence report" in finding


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
    monkeypatch.setattr(artifacts_module, "_resolve_test_body", AsyncMock(return_value=(body, ())))

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
    monkeypatch.setattr(artifacts_module, "_resolve_test_body", AsyncMock(return_value=(body, ())))

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
    monkeypatch.setattr(artifacts_module, "_resolve_test_body", AsyncMock(return_value=(body, ())))

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
        (
            "crates/gcore/tests/schema_contract.rs",
            "commented_contract",
            """
#[test]
fn commented_contract() {
    // assert_eq!(compute(), 2);
    compute();
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
    monkeypatch.setattr(artifacts_module, "_resolve_test_body", AsyncMock(return_value=(body, ())))

    result = evaluate_acceptance_artifacts(
        criteria=f"Contract is executable.\ntest: {path}::{symbol}",
        repo_path=str(tmp_path),
        commit_shas=["linked"],
    )

    assert result.passed is False
    assert result.findings == (f"{path}::{symbol}: {finding}",)


_RUST_HELPER_TEST = """\
#[test]
fn attribute_ranges_python_autouse_fixture() -> anyhow::Result<()> {
    check_declaration(
        "conftest.py",
        "@pytest.fixture(autouse=True)\\ndef clear_identity():\\n    yield\\n",
        "clear_identity",
    )?;
    Ok(())
}
"""


def _evaluate_committed_rust_helper(
    tmp_path: Path, helpers: str
) -> artifacts_module.AcceptanceArtifactResult:
    repo = _init_repo(tmp_path)
    test_file = repo / "crates" / "gcode" / "src" / "attribute_ranges_tests.rs"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(f"use super::*;\n\n{helpers}\n{_RUST_HELPER_TEST}", encoding="utf-8")
    linked_sha = _commit(repo, "committed Rust helper test")
    # The working tree differs from the close candidate; gate 11 must read the commit.
    test_file.write_text(f"use super::*;\n\n{_RUST_HELPER_TEST}", encoding="utf-8")
    return evaluate_acceptance_artifacts(
        criteria=(
            "Ranges include attributes.\ntest: crates/gcode/src/attribute_ranges_tests.rs"
            "::attribute_ranges_python_autouse_fixture"
        ),
        repo_path=str(repo),
        commit_shas=[linked_sha],
    )


_CHECK_DECLARATION_HELPER = """\
fn check_declaration(path: &str, source: &str, name: &str) -> anyhow::Result<()> {
    let parsed = parse_fixture(path, source)?;
    let symbol = parsed.find(name).unwrap_or_else(|| panic!("missing {name}"));
    assert_eq!(symbol.name, name, "{}: {name}", path);
    Ok(())
}
"""


@pytest.mark.parametrize(
    "helpers",
    [
        pytest.param(_CHECK_DECLARATION_HELPER, id="direct-helper"),
        pytest.param(
            "fn check_declaration(path: &str, source: &str, name: &str)"
            " -> anyhow::Result<()> {\n"
            "    assert_symbol(path, source, name)\n}\n\n"
            "fn assert_symbol(path: &str, source: &str, name: &str) -> anyhow::Result<()> {\n"
            '    assert_ne!(source.find(name), None, "{path}");\n    Ok(())\n}\n',
            id="helper-chain",
        ),
        pytest.param(
            "fn check_declaration(path: &str, source: &str, name: &str)"
            " -> anyhow::Result<()> {\n"
            '    let note = "assert!(true)";\n'
            '    assert_ne!(source.find(name), None, "{path}: {note}");\n    Ok(())\n}\n',
            id="placebo-text-inside-a-string",
        ),
    ],
)
def test_rust_test_asserting_through_same_file_helper_is_accepted(
    tmp_path: Path, helpers: str
) -> None:
    result = _evaluate_committed_rust_helper(tmp_path, helpers)

    assert result.findings == ()
    assert result.passed is True
    (test,) = result.tests
    assert any("fn check_declaration" in helper for helper in test.helpers)
    rendered = artifacts_module.render_acceptance_test_bodies(result.tests)
    assert "assert" in rendered.split("fn check_declaration", 1)[1]


_RUST_CHAR_LITERAL_TEST = """\
#[test]
fn splits_on_quote_and_brace_chars() {
    let quote = '"';
    let (open, close, escaped) = ('{', '}', '\\'');
    let raw = r#"{ "unbalanced"#;
    assert_eq!(split_pair(raw, quote), (open, close, escaped));
}
"""


def test_rust_char_literals_do_not_break_braced_body_extraction(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    test_file = repo / "crates" / "gcode" / "src" / "split_tests.rs"
    test_file.parent.mkdir(parents=True)
    trailing = "fn longest<'a>(left: &'a str, right: &'a str) -> &'a str {\n    left\n}\n"
    test_file.write_text(f"{_RUST_CHAR_LITERAL_TEST}\n{trailing}", encoding="utf-8")
    linked_sha = _commit(repo, "committed Rust char literal test")

    result = evaluate_acceptance_artifacts(
        criteria=(
            "Quotes split.\ntest: crates/gcode/src/split_tests.rs::splits_on_quote_and_brace_chars"
        ),
        repo_path=str(repo),
        commit_shas=[linked_sha],
    )

    assert result.findings == ()
    assert result.passed is True
    (test,) = result.tests
    assert test.body == _RUST_CHAR_LITERAL_TEST.rstrip("\n")
    lifetime_body = artifacts_module._extract_braced_test_body(
        test_file.read_text(encoding="utf-8"), "longest", "crates/gcode/src/split_tests.rs"
    )
    assert lifetime_body == trailing.rstrip("\n")


def test_single_quoted_braces_do_not_end_a_typescript_test_body() -> None:
    source = """
it("keeps braces in strings", () => {
  const close = '}'
  expect(close).toBe('}')
})
it("next", () => {})
"""

    body = artifacts_module._extract_braced_test_body(
        source, "keeps braces in strings", "web/src/braces.test.ts"
    )

    assert body.endswith("expect(close).toBe('}')\n}")


@pytest.mark.parametrize(
    ("helpers", "finding"),
    [
        pytest.param(
            "fn check_declaration(path: &str, source: &str, name: &str)"
            " -> anyhow::Result<()> {\n    parse_fixture(path, source)?.find(name);\n"
            "    Ok(())\n}\n",
            "contains no executable assertion or panic expectation",
            id="helper-without-assertion",
        ),
        pytest.param(
            "fn unrelated() {\n    assert_eq!(compute(), 2);\n}\n\n"
            "fn check_declaration(path: &str, source: &str, name: &str)"
            ' -> anyhow::Result<()> {\n    let _ = "unrelated()";\n    Ok(())\n}\n',
            "contains no executable assertion or panic expectation",
            id="asserting-function-named-only-in-a-string",
        ),
        pytest.param(
            "fn check(x: u32) {\n    assert_eq!(x, 1);\n}\n\n"
            "fn check_declaration(path: &str, source: &str, name: &str)"
            " -> anyhow::Result<()> {\n"
            "    let q = '\"'; let s = \"check(1)\"; let r = '\"';\n    Ok(())\n}\n",
            "contains no executable assertion or panic expectation",
            id="call-hidden-between-char-literals",
        ),
        pytest.param(
            "fn check(x: u32) {\n    assert_eq!(x, 1);\n}\n\n"
            "fn check_declaration(path: &str, source: &str, name: &str)"
            " -> anyhow::Result<()> {\n"
            '    let s = r#"a" check(1) "b"#;\n    Ok(())\n}\n',
            "contains no executable assertion or panic expectation",
            id="call-inside-a-raw-string",
        ),
        pytest.param(
            "fn check_declaration(path: &str, source: &str, name: &str)"
            " -> anyhow::Result<()> {\n"
            "    // assert_eq!(name, path);\n    let _ = (path, source, name);\n    Ok(())\n}\n",
            "contains no executable assertion or panic expectation",
            id="commented-out-helper-assertion",
        ),
        pytest.param(
            "fn check_declaration(path: &str, source: &str, name: &str)"
            " -> anyhow::Result<()> {\n    assert!(true);\n    Ok(())\n}\n",
            "contains a constant, stub, or placebo assertion",
            id="placebo-helper",
        ),
    ],
)
def test_rust_test_without_a_real_same_file_assertion_path_is_rejected(
    tmp_path: Path, helpers: str, finding: str
) -> None:
    result = _evaluate_committed_rust_helper(tmp_path, helpers)

    assert result.passed is False
    assert result.findings == (
        "crates/gcode/src/attribute_ranges_tests.rs::attribute_ranges_python_autouse_fixture: "
        f"{finding}",
    )
    # The helper was resolved from the candidate and still earned no credit.
    (test,) = result.tests
    assert any("fn check_declaration" in helper for helper in test.helpers)


_ASSERTING_CHECK_DECLARATION = (
    "fn check_declaration(path: &str, source: &str, name: &str)"
    " -> anyhow::Result<()> {\n    assert_eq!(name, path);\n    Ok(())\n}\n"
)


@pytest.mark.parametrize(
    "helpers",
    [
        pytest.param(f"/*\n{_ASSERTING_CHECK_DECLARATION}*/\n", id="block-comment"),
        pytest.param(
            f'const DOC: &str = r#"\n{_ASSERTING_CHECK_DECLARATION}"#;\n', id="raw-string"
        ),
    ],
)
def test_rust_helper_defined_only_in_a_comment_or_string_is_not_credited(
    tmp_path: Path, helpers: str
) -> None:
    result = _evaluate_committed_rust_helper(tmp_path, helpers)

    assert result.passed is False
    assert result.findings == (
        "crates/gcode/src/attribute_ranges_tests.rs::attribute_ranges_python_autouse_fixture: "
        "contains no executable assertion or panic expectation",
    )
    (test,) = result.tests
    assert test.helpers == ()


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


@pytest.mark.parametrize("repo_subdir", [".", "docs"])
def test_structured_evidence_accepts_locally_provable_run(tmp_path: Path, repo_subdir: str) -> None:
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
        repo_path=str(Path(repo, repo_subdir)),
        commit_shas=[evidence_sha],
    )

    assert findings == ()


def test_file_evidence_reads_the_close_candidate_not_the_latest_link(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    workflow = Path(repo, ".github", "workflows", "weekly.yml")
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: Weekly Producer\non: workflow_dispatch\n", encoding="utf-8")
    cited_sha = _commit(repo, "producer")
    path = Path(repo, "docs", "evidence.md")
    path.parent.mkdir()
    evidence = f"""## Run
- workflow_name: Weekly Producer
- run_url: https://github.com/GobbyAI/gobby/actions/runs/123
- commit_sha: {cited_sha}
- utc_timestamp: 2099-01-01T00:00:00Z
"""
    path.write_text(evidence, encoding="utf-8")
    candidate_sha = _commit(repo, "reviewed evidence")
    # A later link backdates the run before its cited commit, which gate 11 rejects.
    path.write_text(evidence.replace("2099-01-01", "2000-01-01"), encoding="utf-8")
    later_sha = _commit(repo, "later linked commit")

    result = evaluate_acceptance_artifacts(
        criteria="Evidence holds.\nfile: docs/evidence.md",
        repo_path=str(repo),
        commit_shas=[candidate_sha, later_sha],
        candidate_commit_sha=candidate_sha,
    )

    assert result.findings == ()
    assert result.passed is True


def test_file_evidence_without_a_close_candidate_is_refused(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    Path(repo, "docs").mkdir()
    Path(repo, "docs", "evidence.md").write_text("first\n", encoding="utf-8")
    first_sha = _commit(repo, "first link")
    Path(repo, "docs", "evidence.md").write_text("second\n", encoding="utf-8")
    second_sha = _commit(repo, "second link")

    findings = validate_structured_file_evidence(
        evidence_files=("docs/evidence.md",),
        repo_path=str(repo),
        commit_shas=[first_sha, second_sha],
    )

    assert findings == ("docs/evidence.md: an explicit linked close candidate is required",)


def test_file_evidence_missing_at_the_close_candidate_names_it(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    Path(repo, "README.md").write_text("candidate\n", encoding="utf-8")
    candidate_sha = _commit(repo, "candidate without evidence")
    Path(repo, "docs").mkdir()
    # Present in the working tree and a later link, but not at the reviewed candidate.
    Path(repo, "docs", "evidence.md").write_text("later\n", encoding="utf-8")
    later_sha = _commit(repo, "later link adds evidence")

    findings = validate_structured_file_evidence(
        evidence_files=("docs/evidence.md",),
        repo_path=str(repo),
        commit_shas=[candidate_sha, later_sha],
        candidate_commit_sha=candidate_sha,
    )

    assert findings == (
        f"docs/evidence.md: referenced evidence file is missing at close candidate {candidate_sha}",
    )


def test_native_backend_evidence_regression_fails_on_local_contradictions() -> None:
    findings = validate_structured_file_evidence(
        evidence_files=("docs/evidence/native-backend-flip.md",),
        repo_path=str(REPO_ROOT),
        commit_shas=["d07111cf2d", "6b4e032125"],
        candidate_commit_sha="6b4e032125",
    )

    assert any("89f7b404" in finding and "newer" in finding for finding in findings)
    assert any("c62e4bae" in finding and "newer" in finding for finding in findings)
    assert any("89f7b404" in finding and "producer workflow" in finding for finding in findings)


def _fixture_tdd_cycle(
    implementation_path: str, test_path: str = "tests/fixtures/test_binary.py"
) -> tuple[AcceptanceTest, TranscriptEvidence]:
    started = datetime(2026, 8, 21, tzinfo=UTC)
    test = AcceptanceTest(
        reference=f"{test_path}::test_feature",
        path=test_path,
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit(test.path, started, 1),
            _edit(implementation_path, started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            _run(
                test,
                started + timedelta(minutes=1),
                "failure",
                f"FAILED {test.reference}\nE assert 0 == 1",
                2,
            ),
            _run(test, started + timedelta(minutes=3), "success", "1 passed", 4),
        ),
    )
    return test, evidence


@pytest.mark.parametrize("renamed", [False, True])
def test_tdd_named_module_and_rename_alias_never_count_as_implementation(renamed: bool) -> None:
    path = "src/checks.py"
    test, evidence = _fixture_tdd_cycle(path, "src/new_checks.py" if renamed else path)
    aliases = {test.path: (path,)} if renamed else None

    result = evaluate_tdd_evidence((test,), evidence, renamed_test_paths=aliases)

    assert result.passed is False
    assert any("no production edit" in finding for finding in result.findings)


@pytest.mark.parametrize("path", ["conftest.py", "src/conftest.py", "tests/conftest.py"])
def test_tdd_conftest_never_counts_as_implementation(path: str) -> None:
    test, evidence = _fixture_tdd_cycle(path)

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is False
    assert any("no production edit" in finding for finding in result.findings)


def test_tdd_other_named_module_never_counts_as_implementation() -> None:
    path = "src/other_checks.py"
    test, evidence = _fixture_tdd_cycle(path)
    other = replace(test, reference=f"{path}::test_feature", path=path)
    evidence = replace(
        evidence,
        validation_runs=(
            *evidence.validation_runs,
            _run(other, evidence.validation_runs[-1].completed_at, "success", "1 passed", 5),
        ),
    )

    result = evaluate_tdd_evidence((test, other), evidence)

    assert result.passed is False
    assert any("no production edit" in finding for finding in result.findings)


def test_tdd_fixture_deliverable_counts_as_implementation_for_test_task() -> None:
    path = "tests/fixtures/gdaemon_binary.py"
    test, evidence = _fixture_tdd_cycle(path)

    result = evaluate_tdd_evidence(
        (test,), evidence, task_category="test", implementation_paths={path}
    )

    assert result.passed is True
    assert result.red_runs == (f"pytest {test.reference}",)
    assert result.green_runs == (f"pytest {test.reference}",)


@pytest.mark.parametrize(
    "category,delivered", [("code", True), ("refactor", True), (None, True), ("test", False)]
)
def test_tdd_production_task_cannot_use_test_side_edit_alone(
    category: str | None, delivered: bool
) -> None:
    path = "tests/fixtures/gdaemon_binary.py"
    test, evidence = _fixture_tdd_cycle(path)

    result = evaluate_tdd_evidence(
        (test,), evidence, task_category=category, implementation_paths={path} if delivered else ()
    )

    assert result.passed is False
    assert any("no production edit" in finding for finding in result.findings)


@pytest.mark.parametrize(
    "path",
    [
        "tests/fixtures/test_support.py",
        "tests/fixtures/helper_test.py",
        "tests/fixtures/helper.test.ts",
        "tests/fixtures/helper.spec.js",
        "tests/fixtures/tests.rs",
        "tests/fixtures/helper_tests.rs",
        "tests/fixtures/conftest.py",
        "tests/fixtures/data.json",
    ],
)
def test_tdd_test_files_and_data_cannot_be_opted_in_as_fixture_modules(path: str) -> None:
    test, evidence = _fixture_tdd_cycle(path)

    result = evaluate_tdd_evidence(
        (test,), evidence, task_category="test", implementation_paths={path}
    )

    assert result.passed is False
    assert any("no production edit" in finding for finding in result.findings)


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


def test_tdd_evidence_credits_red_before_linked_test_rename() -> None:
    started = datetime(2026, 9, 26, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/new_test_feature.py::test_feature",
        path="tests/new_test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    old_test = replace(
        test,
        reference="tests/old_test_feature.py::test_feature",
        path="tests/old_test_feature.py",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit(old_test.path, started, 1),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            _run(
                old_test,
                started + timedelta(minutes=1),
                "failure",
                "FAILED tests/old_test_feature.py::test_feature\nE assert 0 == 1",
                2,
            ),
            _run(
                test,
                started + timedelta(minutes=3),
                "success",
                "tests/new_test_feature.py::test_feature PASSED",
                4,
            ),
        ),
    )

    assert not evaluate_tdd_evidence((test,), evidence).passed
    result = evaluate_tdd_evidence(
        (test,), evidence, renamed_test_paths={test.path: (old_test.path,)}
    )
    assert result.passed
    assert result.red_runs == ("pytest tests/old_test_feature.py::test_feature",)
    assert result.green_runs == ("pytest tests/new_test_feature.py::test_feature",)


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


def test_tdd_evidence_credits_tb_line_failure_for_sole_selected_node() -> None:
    started = datetime(2026, 9, 28, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/config/test_persistence.py::test_removed_memory_keys_raise",
        path="tests/config/test_persistence.py",
        symbol="test_removed_memory_keys_raise",
        body="def test_removed_memory_keys_raise(): assert key not in fields",
    )
    edits = (
        _edit("tests/config/test_persistence.py", started, 1),
        _edit("src/gobby/config/persistence.py", started + timedelta(minutes=2), 3),
    )
    green = _run(test, started + timedelta(minutes=3), "success", "1 passed", 4)
    # pytest --tb=line prints the location without the test symbol (#22839 red).
    tb_line_failure = (
        "tests/config/test_persistence.py F\n"
        "=================================== FAILURES ===================================\n"
        "E   AssertionError: assert 'recall_signal_hub' not in {...}\n"
        "/repo/tests/config/test_persistence.py:145: AssertionError: "
        "assert 'recall_signal_hub' not in {...}\n"
        "============================== 1 failed in 1.20s ==============================\n"
    )

    accepted = evaluate_tdd_evidence(
        (test,),
        TranscriptEvidence(
            edits=edits,
            validation_runs=(
                _run(test, started + timedelta(minutes=1), "failure", tb_line_failure, 2),
                green,
            ),
        ),
    )

    assert accepted.passed is True
    assert accepted.red_runs

    sibling = replace(
        _run(test, started + timedelta(minutes=1), "failure", tb_line_failure, 2),
        command=f"pytest {test.reference} tests/config/test_persistence.py::test_other",
    )
    ambiguous = evaluate_tdd_evidence(
        (test,), TranscriptEvidence(edits=edits, validation_runs=(sibling, green))
    )
    assert ambiguous.passed is False
    assert "no attributable failure section" in ambiguous.findings[0]


@pytest.mark.parametrize(
    ("red_command", "failure_detail", "failure_location", "sibling_path", "sibling_node"),
    [
        (
            "pytest tests/test_feature.py -q --tb=line",
            "E   KeyError: 'new_field'",
            "/repo/tests/test_feature.py:24: KeyError: 'new_field'",
            "/repo/tests/test_feature.py:40",
            "tests/test_feature.py::test_sibling",
        ),
        (
            "pytest tests/test_feature.py::test_feature tests/test_other.py -q --tb=line",
            "E   ImportError: cannot import name 'new_api' from 'feature'",
            "/repo/tests/test_feature.py:24: ImportError: cannot import name 'new_api' from 'feature'",
            "/repo/tests/test_other.py:10",
            "tests/test_other.py::test_other",
        ),
        (
            "pytest tests/test_feature.py tests/test_other.py -q --tb=line",
            "E   assert False",
            "/repo/tests/test_feature.py:24: assert False",
            "/repo/tests/test_other.py:10",
            "tests/test_other.py::test_other",
        ),
    ],
)
def test_tdd_evidence_credits_tb_line_summary_with_body_exception(
    red_command: str,
    failure_detail: str,
    failure_location: str,
    sibling_path: str,
    sibling_node: str,
) -> None:
    started = datetime(2026, 9, 29, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    red_output = (
        "=================================== FAILURES ===================================\n"
        f"{failure_detail}\n"
        f"{failure_location}\n"
        "E   ImportError: unrelated sibling failure\n"
        f"{sibling_path}: ImportError: unrelated sibling failure\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_feature.py::test_feature\n"
        f"FAILED {sibling_node}\n"
        "======================== 2 failed in 0.10s ========================\n"
    )
    red = replace(
        _run(test, started + timedelta(minutes=1), "failure", red_output, 2),
        command=red_command,
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit(test.path, started, 1),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            red,
            _run(test, started + timedelta(minutes=3), "success", "1 passed", 4),
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is True
    assert result.red_runs == (red_command,)


def test_tdd_evidence_does_not_borrow_tb_line_detail_from_sibling_summary() -> None:
    started = datetime(2026, 9, 29, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    red_output = (
        "=================================== FAILURES ===================================\n"
        "E   AssertionError: assert 0 == 1\n"
        "/repo/tests/test_feature.py:40: AssertionError: assert 0 == 1\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_feature.py::test_feature\n"
        "FAILED tests/test_feature.py::test_sibling\n"
    )
    red = replace(
        _run(test, started + timedelta(minutes=1), "failure", red_output, 2),
        command="pytest tests/test_feature.py -q --tb=line",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit(test.path, started, 1),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            red,
            _run(test, started + timedelta(minutes=3), "success", "1 passed", 4),
        ),
    )

    assert evaluate_tdd_evidence((test,), evidence).passed is False


def test_tdd_evidence_does_not_borrow_sibling_assertion_after_summary_rejection() -> None:
    started = datetime(2026, 9, 29, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    red_output = (
        "=================================== FAILURES ===================================\n"
        "E   Failed: Timeout (>30.0s) from pytest-timeout.\n"
        "/repo/tests/test_feature.py:24: Failed: Timeout (>30.0s) from pytest-timeout.\n"
        "E   AssertionError: assert 0 == 1\n"
        "/repo/tests/test_other.py:10: AssertionError: assert 0 == 1\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_feature.py::test_feature\n"
        "FAILED tests/test_other.py::test_other\n"
    )
    red = replace(
        _run(test, started + timedelta(minutes=1), "failure", red_output, 2),
        command="pytest tests/test_feature.py::test_feature tests/test_other.py -q --tb=line",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit(test.path, started, 1),
            _edit("src/feature.py", started + timedelta(minutes=2), 3),
        ),
        validation_runs=(
            red,
            _run(test, started + timedelta(minutes=3), "success", "1 passed", 4),
        ),
    )

    assert evaluate_tdd_evidence((test,), evidence).passed is False


def test_python_postproduction_red_cannot_borrow_sibling_notimplemented() -> None:
    started = datetime(2026, 9, 26, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body="def test_feature(): assert feature() == 1",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit(test.path, started, 1),
            _edit("src/feature.py", started + timedelta(minutes=1), 2),
            _edit("src/feature.py", started + timedelta(minutes=3), 4),
        ),
        validation_runs=(
            _run(
                test,
                started + timedelta(minutes=2),
                "failure",
                "____ test_feature ____\ntests/test_feature.py:5: in test_feature\nE   AssertionError\n"
                "____ test_other ____\ntests/test_feature.py:10: in test_other\nE   NotImplementedError\n",
                3,
            ),
            _run(test, started + timedelta(minutes=4), "success", "2 passed", 5),
        ),
    )
    result = evaluate_tdd_evidence((test,), evidence)
    assert result.passed is False, result


@pytest.mark.parametrize("failure_in_sibling", [False, True])
def test_tdd_evidence_credits_function_local_import_of_new_module_stub(
    failure_in_sibling: bool,
) -> None:
    started = datetime(2026, 10, 6, tzinfo=UTC)
    body = (
        "def test_credential_is_owner_only_and_never_rewritten(tmp_path):\n"
        "    from gobby.utils.break_glass import ensure_break_glass_credential\n"
        "    ensure_break_glass_credential(tmp_path)\n"
        "    assert (tmp_path / 'break_glass').is_file()\n"
    )
    test = AcceptanceTest(
        reference="tests/utils/test_break_glass.py::test_credential_is_owner_only_and_never_rewritten",
        path="tests/utils/test_break_glass.py",
        symbol="test_credential_is_owner_only_and_never_rewritten",
        body=body,
    )
    # Same API-only module shape as the original break-glass RED, with its
    # NotImplementedError at line 14 and an import inside the named test body.
    stub = (
        "from pathlib import Path\n"
        + "\n" * 11
        + (
            "def ensure_break_glass_credential(home: Path) -> None:\n"
            "    raise NotImplementedError\n"
        )
    )
    red = replace(
        _run(
            test,
            started + timedelta(minutes=2),
            "failure",
            "Pytest: 0 passed, 1 failed\nFailures:\n"
            "1. [FAIL] test_credential_is_owner_only_and_never_rewritten\n"
            "    tests/utils/test_break_glass.py:3: in "
            "test_credential_is_owner_only_and_never_rewritten\n"
            + (
                "2. [FAIL] test_sibling\n    tests/utils/test_break_glass.py:40: in test_sibling\n"
                if failure_in_sibling
                else ""
            )
            + "    src/gobby/utils/break_glass.py:14: in ensure_break_glass_credential\n"
            "    raise NotImplementedError\n",
            3,
        ),
        command="pytest tests/utils/test_break_glass.py -q",
    )
    evidence = TranscriptEvidence(
        edits=(
            replace(
                _edit(test.path, started, 1),
                source_after=body,
                source_confirmed=True,
                source_confirmed_at=started + timedelta(seconds=1),
            ),
            replace(
                _edit("src/gobby/utils/break_glass.py", started + timedelta(minutes=1), 2),
                source_after=stub,
                source_created=True,
                source_confirmed=True,
                source_confirmed_at=started + timedelta(seconds=61),
            ),
            _edit("src/gobby/utils/break_glass.py", started + timedelta(minutes=3), 4),
        ),
        validation_runs=(
            red,
            _run(test, started + timedelta(minutes=4), "success", "1 passed", 5),
        ),
    )

    result = evaluate_tdd_evidence((test,), evidence)
    assert result.passed is not failure_in_sibling, result.findings
    assert result.red_runs == (() if failure_in_sibling else (red.command,))


def test_tdd_evidence_credits_rtk_not_implemented_raise_after_stub_edit() -> None:
    started = datetime(2026, 9, 26, tzinfo=UTC)
    body = "from feature import feature\n\ndef test_feature():\n    assert feature() == 1\n"
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body=body,
    )
    edits = (
        replace(
            _edit(test.path, started, 1),
            source_after=body,
            source_confirmed=True,
            source_confirmed_at=started + timedelta(seconds=1),
        ),
        replace(
            _edit("src/feature.py", started + timedelta(minutes=1), 2),
            source_after="def feature():\n    raise NotImplementedError\n",
            source_confirmed=True,
            source_created=True,
            source_confirmed_at=started + timedelta(seconds=61),
        ),
        _edit("src/feature.py", started + timedelta(minutes=3), 4),
    )
    location_only = """\
Pytest: 0 passed, 1 failed
Failures:
1. [FAIL] test_feature
    tests/test_feature.py:9: in test_feature
    src/feature.py:3: in feature
"""
    green = _run(test, started + timedelta(minutes=4), "success", "1 passed", 5)

    def evaluate(red_output: str) -> TddEvidenceResult:
        return evaluate_tdd_evidence(
            (test,),
            TranscriptEvidence(
                edits=edits,
                validation_runs=(
                    _run(test, started + timedelta(minutes=2), "failure", red_output, 3),
                    green,
                ),
            ),
        )

    assert not evaluate(location_only).passed
    result = evaluate(location_only + "    raise NotImplementedError\n")
    assert result.passed
    assert result.red_runs == ("pytest tests/test_feature.py::test_feature",)


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


_HELPER_FAIL_BODY = """\
import pytest

from feature import feature


def test_feature(monkeypatch):
    def unexpected_render(**_context):
        pytest.fail("no policy may be rendered for a swapped cache grant")

    monkeypatch.setattr("feature.render", unexpected_render)
    with pytest.raises(PermissionError):
        feature()
"""
_CONDITIONAL_FAIL_BODY = """\
import pytest

from feature import feature


def test_feature():
    if feature():
        return
    pytest.fail("feature false")
"""
_PLACEHOLDER_FAIL_BODY = """\
import pytest


def test_feature():
    pytest.fail("not written yet")
"""


def _tb_line_red(
    detail: str, *, node: str = "tests/test_feature.py::test_feature", line: int = 9
) -> str:
    return (
        "=================================== FAILURES ===================================\n"
        f"E   {detail}\n"
        f"/repo/tests/test_feature.py:{line}: {detail}\n"
        "=========================== short test summary info ============================\n"
        f"FAILED {node}\n"
        "============================== 1 failed in 0.10s ===============================\n"
    )


@pytest.mark.parametrize(
    "body,red_output,red_minute,expected",
    [
        pytest.param(
            _HELPER_FAIL_BODY,
            _tb_line_red("Failed: no policy may be rendered for a swapped cache grant"),
            2,
            None,
            id="helper-pytest-fail",
        ),
        pytest.param(
            _CONDITIONAL_FAIL_BODY,
            _tb_line_red("Failed: feature false"),
            2,
            None,
            id="fail-after-success-return",
        ),
        pytest.param(
            _HELPER_FAIL_BODY,
            _tb_line_red("Failed: Timeout (>30.0s) from pytest-timeout."),
            2,
            "no attributable failure section",
            id="timeout",
        ),
        pytest.param(
            _PLACEHOLDER_FAIL_BODY,
            _tb_line_red("Failed: not written yet"),
            2,
            "unconditional pytest.fail placeholder",
            id="placeholder",
        ),
        *[
            pytest.param(
                "import pytest\n\ndef test_feature():\n"
                + padding
                + '    pytest.fail("not written yet")\n',
                _tb_line_red("Failed: not written yet"),
                2,
                "unconditional pytest.fail placeholder",
                id=f"placeholder-{name}",
            )
            for name, padding in (
                ("assignment", "    marker = 0\n"),
                ("pass", "    pass\n"),
                ("docstring-assignment", '    "Pending test."\n    marker = 0\n'),
                ("builtin-call", '    print("pending")\n'),
                ("stdlib-call", "    import time as clock\n    clock.monotonic()\n"),
                ("uncalled-helper", "    def helper():\n        feature()\n"),
                ("uncalled-lambda", "    helper = lambda: feature()\n"),
            )
        ],
        pytest.param(
            "import pytest\nfrom feature import feature\n\ndef test_feature():\n"
            '    feature()\n    pytest.fail("feature returned unexpectedly")\n',
            _tb_line_red("Failed: feature returned unexpectedly"),
            2,
            None,
            id="fail-after-production-call",
        ),
        *[
            pytest.param(
                "import pytest\n\ndef test_feature():\n    " + statement + "\n",
                _tb_line_red("Failed: not written yet"),
                2,
                "unconditional pytest.fail placeholder",
                id=f"placeholder-expression-{name}",
            )
            for name, statement in (
                ("assign", 'result = pytest.fail("not written yet")'),
                ("annassign", 'result: object = pytest.fail("not written yet")'),
                ("augassign", 'result += pytest.fail("not written yet")'),
                ("return", 'return pytest.fail("not written yet")'),
                ("raise", 'raise pytest.fail("not written yet")'),
                ("nested-call", 'print(pytest.fail("not written yet"))'),
                ("application-argument", 'feature(pytest.fail("not written yet"))'),
                ("tuple", '(pytest.fail("not written yet"), feature())'),
                ("walrus", '(result := pytest.fail("not written yet"))'),
                ("if-test", 'if pytest.fail("not written yet"):\n        pass'),
                ("while-test", 'while pytest.fail("not written yet"):\n        pass'),
                ("assert-test", 'assert pytest.fail("not written yet")'),
                ("bool-first", 'pytest.fail("not written yet") or feature()'),
                ("compare-chain-left", 'pytest.fail("not written yet") < 1 < feature()'),
                ("compare-chain-first", '0 < pytest.fail("not written yet") < feature()'),
                (
                    "bare-pytest-decorator",
                    "@pytest.fixture\n    def pending():\n        feature()\n"
                    '    pytest.fail("not written yet")',
                ),
                ("with-context", 'with pytest.fail("not written yet"):\n        pass'),
                ("assign-target", 'marker[feature()] = pytest.fail("not written yet")'),
                ("assign-attribute", 'feature().marker = pytest.fail("not written yet")'),
                ("assign-targets", 'marker = feature().marker = pytest.fail("not written yet")'),
                ("annassign-target", 'marker[feature()]: object = pytest.fail("not written yet")'),
                (
                    "annassign-attribute",
                    'feature().marker: object = pytest.fail("not written yet")',
                ),
                ("local-annotation", 'marker: feature() = 0\n    pytest.fail("not written yet")'),
                ("local-annotation-only", 'marker: feature()\n    pytest.fail("not written yet")'),
                ("dict-values", 'result = {0: pytest.fail("not written yet"), feature(): 1}'),
                ("dict-unpack", 'result = {**{0: pytest.fail("not written yet")}, feature(): 1}'),
                (
                    "augassign-value",
                    'marker[0] += {0: pytest.fail("not written yet"), feature(): 1}',
                ),
                ("keyword-value", 'feature(key={0: pytest.fail("not written yet"), feature(): 1})'),
                ("starred-value", 'feature(*{0: pytest.fail("not written yet"), feature(): 1})'),
                (
                    "listcomp-iterator",
                    'result = [x for x in {0: pytest.fail("not written yet"), feature(): 1}]',
                ),
                (
                    "setcomp-iterator",
                    'result = {x for x in {0: pytest.fail("not written yet"), feature(): 1}}',
                ),
                (
                    "dictcomp-iterator",
                    'result = {x: x for x in {0: pytest.fail("not written yet"), feature(): 1}}',
                ),
                (
                    "generator-iterator",
                    'result = (x for x in {0: pytest.fail("not written yet"), feature(): 1})',
                ),
                (
                    "generator-body",
                    'pending = (feature() for _ in ())\n    pytest.fail("not written yet")',
                ),
                (
                    "generator-later-iterator",
                    "pending = (x for x in () for y in feature())\n"
                    '    pytest.fail("not written yet")',
                ),
                (
                    "generator-filter",
                    'pending = (x for x in () if feature())\n    pytest.fail("not written yet")',
                ),
                (
                    "lambda-default",
                    'pending = lambda x={0: pytest.fail("not written yet"), feature(): 1}: x',
                ),
                (
                    "nested-def-default",
                    'def pending(x={0: pytest.fail("not written yet"), feature(): 1}):\n'
                    "        feature()",
                ),
                (
                    "nested-def-keyword-default",
                    'def pending(*, x={0: pytest.fail("not written yet"), feature(): 1}):\n'
                    "        feature()",
                ),
                (
                    "nested-async-def-default",
                    'async def pending(x={0: pytest.fail("not written yet"), feature(): 1}):\n'
                    "        feature()",
                ),
                (
                    "class-decorator",
                    '@pytest.fail("not written yet")\n    class Pending:\n'
                    "        marker = feature()",
                ),
                (
                    "type-alias",
                    'type Pending = feature()\n    pytest.fail("not written yet")',
                ),
            )
        ],
        *[
            pytest.param(
                "import pytest\nfrom feature import feature\n\ndef test_feature():\n"
                "    marker = {0: 0}\n    "
                + statement
                + '\n    pytest.fail("feature returned unexpectedly")\n',
                _tb_line_red("Failed: feature returned unexpectedly"),
                2,
                None,
                id=f"evaluated-application-{name}",
            )
            for name, statement in (
                ("annassign-value", "marker: object = feature()"),
                ("annassign-target", "feature().marker: object = 0"),
                ("augassign-subscript", "marker[feature()] += 0"),
                ("augassign-attribute", "feature().marker += 0"),
                ("dict-key", "marker = {feature(): 0}"),
                ("dict-value", "marker = {0: feature()}"),
                ("keyword", "dict(key=feature())"),
                ("starred-before-keyword", 'dict(key=pytest.fail("later"), *(feature(),))'),
                ("keyword-after-starred", "dict(*(), key=feature())"),
                ("generator-iterator", "pending = (x for x in feature())"),
                ("listcomp-iterator", "marker = [x for x in feature()]"),
                ("setcomp-iterator", "marker = {x for x in feature()}"),
                ("dictcomp-iterator", "marker = {x: x for x in feature()}"),
                ("lambda-default", "pending = lambda x=feature(): x"),
                ("nested-def-default", "def pending(x=feature()):\n        return x"),
                ("compare-chain-later", '1 > 2 < pytest.fail("later")\n    feature()'),
                ("bare-decorator", "@feature\n    def pending():\n        pass"),
                ("bare-attribute-decorator", "@feature.wrap\n    def pending():\n        pass"),
                ("bare-class-decorator", "@feature\n    class Pending:\n        pass"),
            )
        ],
        pytest.param(
            "import pytest\nfrom feature import feature\n\ndef test_feature():\n"
            '    result = pytest.fail(f"feature returned {feature()}")\n',
            _tb_line_red("Failed: feature returned False"),
            2,
            None,
            id="assigned-fail-evaluates-production-call",
        ),
        pytest.param(
            "import pytest\nfrom feature import feature\n\ndef test_feature():\n"
            '    pytest.fail(f"feature returned {feature()}")\n',
            _tb_line_red("Failed: feature returned False"),
            2,
            None,
            id="fail-evaluates-production-call",
        ),
        *[
            pytest.param(
                imports
                + "\n\ndef test_feature():\n    marker = 0\n"
                + f'    {fail_call}("not written yet")\n',
                _tb_line_red("Failed: not written yet"),
                2,
                "unconditional pytest.fail placeholder",
                id=f"placeholder-{name}",
            )
            for name, imports, fail_call in (
                ("pytest-alias", "import pytest as pt", "pt.fail"),
                ("fail-alias", "from pytest import fail as abort", "abort"),
            )
        ],
        pytest.param(
            "from feature import fail\n\ndef test_feature():\n    fail()\n",
            _tb_line_red("Failed: production failure"),
            2,
            None,
            id="application-call-named-fail",
        ),
        pytest.param(
            _HELPER_FAIL_BODY,
            _tb_line_red(
                "Failed: no policy may be rendered for a swapped cache grant",
                node="tests/test_feature.py::test_other",
                line=30,
            ),
            2,
            "no attributable failure section",
            id="summary-names-another-test",
        ),
        pytest.param(
            _HELPER_FAIL_BODY,
            "==================================== ERRORS ====================================\n"
            "E   Failed: fixture 'unknown' not found\n"
            "=========================== short test summary info ============================\n"
            "ERROR tests/test_feature.py::test_feature\n"
            "=============================== 1 error in 0.10s ===============================\n",
            2,
            "missing assertion or panic failure",
            id="setup-error",
        ),
        pytest.param(
            _HELPER_FAIL_BODY,
            _tb_line_red("Failed: no policy may be rendered for a swapped cache grant"),
            4,
            "missing assertion or panic failure",
            id="after-implementation",
        ),
    ],
)
def test_tdd_evidence_credits_pytest_fail_red_only_from_an_exercising_body(
    body: str, red_output: str, red_minute: int, expected: str | None
) -> None:
    started = datetime(2026, 10, 1, tzinfo=UTC)
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body=body,
    )
    red = replace(
        _run(test, started + timedelta(minutes=red_minute), "failure", red_output, red_minute),
        command=(
            "pytest tests/test_feature.py::test_feature tests/test_feature.py::test_other "
            "-q --tb=line"
        ),
    )
    evidence = TranscriptEvidence(
        edits=(
            replace(
                _edit(test.path, started, 1),
                source_after=body,
                source_confirmed=True,
                source_confirmed_at=started + timedelta(seconds=1),
            ),
            _edit("src/feature.py", started + timedelta(minutes=3), 3),
        ),
        validation_runs=(red, _run(test, started + timedelta(minutes=5), "success", "1 passed", 5)),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    if expected is None:
        assert result.passed is True, result
        assert result.red_runs == (red.command,)
    else:
        assert result.passed is False
        assert any(expected in finding for finding in result.findings), result.findings


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
    body = "from feature import feature\n\ndef test_feature():\n    assert feature() == 1\n"
    test = AcceptanceTest(
        reference="tests/test_feature.py::test_feature",
        path="tests/test_feature.py",
        symbol="test_feature",
        body=body,
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
            replace(
                _edit(test.path, started, 1),
                source_after=body,
                source_confirmed=True,
                source_confirmed_at=started + timedelta(seconds=1),
            ),
            replace(
                _edit("src/feature.py", started + timedelta(seconds=30), 2),
                source_after="def feature():\n    raise NotImplementedError\n",
                source_confirmed=True,
                source_created=True,
                source_confirmed_at=started + timedelta(seconds=31),
            ),
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


@pytest.mark.parametrize(
    "red_detail",
    ["assertion failed: description was cut", "panicked at 'not yet implemented'"],
)
def test_tdd_evidence_credits_rust_stub_first_red(red_detail: str) -> None:
    started = datetime(2026, 9, 26, tzinfo=UTC)
    symbol = "the_description_column_is_never_cut_at_narrow_widths"
    test = AcceptanceTest(
        reference=f"crates/gclient/tests/keybind_help.rs::{symbol}",
        path="crates/gclient/tests/keybind_help.rs",
        symbol=symbol,
        body=f"fn {symbol}() {{ assert!(description_is_visible()); }}",
    )
    command = (
        "CARGO_BUILD_JOBS=2 cargo nextest run -p gobby-client --test keybind_help "
        f"-E 'test({symbol})' --test-threads 2"
    )
    red = replace(
        _run(
            test,
            started + timedelta(minutes=2),
            "failure",
            f"FAIL gobby-client::keybind_help {symbol}\n{red_detail}",
            3,
        ),
        command=command,
        matcher_id="cargo-nextest",
        label="cargo nextest",
    )
    green = replace(
        _run(
            test,
            started + timedelta(minutes=4),
            "success",
            f"PASS gobby-client::keybind_help {symbol}",
            5,
        ),
        command=command,
        matcher_id="cargo-nextest",
        label="cargo nextest",
    )
    evidence = TranscriptEvidence(
        edits=(
            _edit(test.path, started, 1),
            _edit("crates/gclient/src/ui/keybind_help.rs", started + timedelta(minutes=1), 2),
            _edit("crates/gclient/src/ui/keybind_help.rs", started + timedelta(minutes=3), 4),
        ),
        validation_runs=(red, green),
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is True
    assert result.red_runs == (command,)
    assert result.green_runs == (command,)

    reconstructed_red = replace(evidence, edits=evidence.edits[:2])
    assert evaluate_tdd_evidence((test,), reconstructed_red).passed is False


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
        ("crates/gterminal/src/host/backpressure/tests.rs", True),
        ("crates/gcore/src/ai/tests.rs", True),
        ("crates/gcode/src/communities/remap_tests.rs", True),
        ("src/tests.rs", True),
        ("crates/gcore/benches/tests.rs", False),
        ("crates/gcore/src/backpressure.rs", False),
        ("src/gobby/tasks/tdd_evidence.py", False),
    ],
)
def test_test_convention_paths_cover_every_language_and_test_tree(
    path: str, convention: bool
) -> None:
    assert is_test_convention_path(path) is convention
