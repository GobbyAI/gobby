"""Tests for deterministic plan semantic lint."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from gobby.plans.parser import parse_plan
from gobby.plans.semantic_lint import SemanticLintResult, lint_plan_document

pytestmark = pytest.mark.unit


def _write_plan(tmp_path: Path, deliverable_body: str) -> Path:
    path = tmp_path / "plan.md"
    header = textwrap.dedent(
        """
        > **Plan ID:** semantic-lint

        # Semantic Lint

        ## P1: Work
        `kind: framing`

        ### 1.1 Implement Work [category: code]
        `kind: deliverable`
        """
    ).lstrip()
    path.write_text(
        header + "\n" + textwrap.dedent(deliverable_body).strip() + "\n",
        encoding="utf-8",
    )
    return path


def _lint(tmp_path: Path, deliverable_body: str) -> list[str]:
    return lint_plan_document(
        parse_plan(_write_plan(tmp_path, deliverable_body), parse_mode="draft")
    ).errors


def _lint_plan_text(
    tmp_path: Path,
    plan_text: str,
    *,
    project_root: Path | None = None,
) -> SemanticLintResult:
    plan_path = tmp_path / "multi-section-plan.md"
    plan_path.write_text(textwrap.dedent(plan_text).lstrip(), encoding="utf-8")
    plan_doc = parse_plan(plan_path, parse_mode="draft")
    return lint_plan_document(plan_doc, project_root=project_root)


def test_complete_target_inventory_passes(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Targets:
        - `src/app.py`
        - `tests/test_app.py`

        Update `src/app.py`.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        - 1.1.2 - Regression is covered. test: `tests/test_app.py::test_app`.
        """,
    )

    assert errors == []


def test_missing_body_path_fails(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        Update `src/other.py` from the same section.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        """,
    )

    assert any("target-coverage" in error for error in errors)
    assert any("src/other.py" in error for error in errors)


def test_acceptance_test_paths_do_not_require_targets(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        Update app behavior.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        - 1.1.2 - Regression is covered. test: `tests/test_app.py::test_app`.
        """,
    )

    assert errors == []


def test_acceptance_file_paths_still_require_targets(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        Update app behavior.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        - 1.1.2 - Helper exists. file: `src/helper.py`.
        """,
    )

    assert any("src/helper.py" in error for error in errors)


def test_body_context_paths_do_not_require_targets(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        Read `src/context.py` for the current schema shape, then update the target file.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        """,
    )

    assert errors == []


def test_basename_mentions_are_covered_by_full_target_paths(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/gobby/install/shared/registry/stages.yaml`

        Update `stages.yaml` with the new stage row.

        **Acceptance:**
        - 1.1.1 - Registry changes. file: `src/gobby/install/shared/registry/stages.yaml`.
        """,
    )

    assert errors == []


def test_dotted_symbols_do_not_emit_fake_path_tokens(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        Update hashing via hashlib.sha256 without changing imports.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        """,
    )

    assert not any("hashlib.sh" in error for error in errors)


def test_target_coverage_error_states_the_block_format_rule(tmp_path: Path) -> None:
    """The block format is only recoverable from source unless the remedy says it."""
    errors = _lint(
        tmp_path,
        """
        Targets:

        - `src/app.py`

        Update `src/app.py`.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        """,
    )

    assert len(errors) == 1
    message = errors[0]
    assert "src/app.py" in message
    assert "no blank line between them" in message
    assert "a blank line ends the block" in message
    assert "must match a target entry exactly" in message
    assert "docs/contracts/plan-coverage.md" in message


def test_bare_extensions_are_not_concrete_paths(tmp_path: Path) -> None:
    """`.tsx` names a file type; only a named file can appear in a target inventory."""
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        Update `src/app.py`; non-test `.ts`, `.tsx`, and `.css` files stay
        under 1,000 lines, and `web/src/.tsx` is not a file either.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        """,
    )

    assert errors == []


def test_dotfiles_with_extensions_are_concrete_paths(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        Update `.impeccable.md` alongside `src/app.py`.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        """,
    )

    assert any(".impeccable.md" in error for error in errors)


def test_multiple_targets_entries_parse(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Targets:
        - `src/app.py`
        - `tests/test_app.py`

        Also keep `docs/app.md` synced.
        Target: `docs/app.md`

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        - 1.1.2 - Regression is covered. test: `tests/test_app.py::test_app`.
        - 1.1.3 - Docs are updated. file: `docs/app.md`.
        """,
    )

    assert errors == []


def test_target_header_rest_keeps_following_bullets_in_same_inventory(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Targets: `src/app.py`
        - `docs/app.md`

        Update `src/app.py` and `docs/app.md` together.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        - 1.1.2 - Docs are updated. file: `docs/app.md`.
        """,
    )

    assert errors == []


def test_work_table_with_too_few_acceptance_items_fails(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        | Work |
        | --- |
        | parser |
        | manifest |
        | cli |

        **Acceptance:**
        - 1.1.1 - Parser exists. file: `src/app.py`.
        """,
    )

    assert any("table-row-decomposition" in error for error in errors)
    assert any("missing rows: 2" in error for error in errors)


def test_schema_reference_table_does_not_false_positive(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        | Column | Type | Description |
        | --- | --- | --- |
        | id | text | Identifier |
        | status | text | Current state |

        **Acceptance:**
        - 1.1.1 - Model exists. file: `src/app.py`.
        """,
    )

    assert errors == []


def test_line_anchored_file_refs_match_targets(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        Update `src/app.py:10-12` for the new behavior.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py:10`.
        """,
    )

    assert errors == []


def test_wire_verbs_count_as_change_intent(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        Wire `src/wiring.py` into the app startup path.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        """,
    )

    assert any("src/wiring.py" in error for error in errors)


def test_file_header_table_counts_as_work_table(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        | File | Change |
        | --- | --- |
        | src/app.py | parser |
        | src/cli.py | command |

        **Acceptance:**
        - 1.1.1 - Parser exists. file: `src/app.py`.
        """,
    )

    assert any("table-row-decomposition" in error for error in errors)


def test_mjs_and_cjs_paths_are_concrete_targets(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Targets:
        - `src/scripts/detect.mjs`
        - `src/scripts/runner.cjs`

        Update `src/scripts/detect.mjs` and `src/scripts/runner.cjs`.

        **Acceptance:**
        - 1.1.1 - Detector behavior exists. file: `src/scripts/detect.mjs`.
        """,
    )

    assert errors == []


def test_mjs_body_mention_without_target_fails(tmp_path: Path) -> None:
    errors = _lint(
        tmp_path,
        """
        Target: `src/app.py`

        Update `src/scripts/detect.mjs` alongside the app.

        **Acceptance:**
        - 1.1.1 - App behavior exists. file: `src/app.py`.
        """,
    )

    assert any("target-coverage" in error for error in errors)
    assert any("src/scripts/detect.mjs" in error for error in errors)


def test_shared_target_ordering_reports_each_unordered_pair_once(tmp_path: Path) -> None:
    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** shared-target-ordering

        # Shared Target Ordering

        ## P1: Work
        `kind: framing`

        ### 1.1 First owner [category: code]
        `kind: deliverable`

        Target: `src/shared.py::first`

        Implement the first owner.

        **Acceptance:**
        - 1.1.1 - First owner is complete. file: `src/shared.py`.

        ### 1.2 Second owner [category: code]
        `kind: deliverable`

        Target: `src/shared.py::second`

        Implement the second owner.

        **Acceptance:**
        - 1.2.1 - Second owner is complete. file: `src/shared.py`.
        """,
    )

    issues = [issue for issue in result.issues if issue.code == "shared-target-ordering"]
    assert len(issues) == 1
    assert issues[0].details == {
        "file_path": "src/shared.py",
        "sections": ["1.1", "1.2"],
    }


def test_shared_target_ordering_accepts_phase_dependency(tmp_path: Path) -> None:
    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** shared-target-phase-ordering

        # Shared Target Phase Ordering

        ## P1: Foundation
        `kind: framing`

        ### 1.1 First owner [category: code]
        `kind: deliverable`

        Target: `src/shared.py::first`

        Implement the first owner.

        **Acceptance:**
        - 1.1.1 - First owner is complete. file: `src/shared.py`.

        ## P2: Follow-up
        `kind: framing`

        ### 2.1 Second owner [category: code] (depends: P1)
        `kind: deliverable`

        Target: `src/shared.py::second`

        Implement the second owner.

        **Acceptance:**
        - 2.1.1 - Second owner is complete. file: `src/shared.py`.
        """,
    )

    assert not any(issue.code == "shared-target-ordering" for issue in result.issues)


def test_production_size_growth_reports_threshold_and_ceiling(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "large.py"
    source_path.parent.mkdir()
    source_path.write_text("value = 1\n" * 850, encoding="utf-8")

    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** production-size-growth

        # Production Size Growth

        ## P1: Work
        `kind: framing`

        ### 1.1 Extend large module [category: code]
        `kind: deliverable`

        Target: `src/large.py::run`

        Implement more behavior.

        **Acceptance:**
        - 1.1.1 - Behavior is complete. file: `src/large.py`.
        """,
        project_root=tmp_path,
    )

    issues = [issue for issue in result.issues if issue.code == "production-size-growth"]
    assert len(issues) == 1
    assert "850 lines" in issues[0].message
    assert "1,000" in issues[0].message


def test_production_size_growth_accepts_explicit_new_split_target(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "large.py"
    source_path.parent.mkdir()
    source_path.write_text("value = 1\n" * 850, encoding="utf-8")

    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** production-size-split

        # Production Size Split

        ## P1: Work
        `kind: framing`

        ### 1.1 Split large module [category: code]
        `kind: deliverable`

        Targets:
        - `src/large.py::run`
        - `src/large_helpers.py`

        Split `src/large.py` and move helpers into `src/large_helpers.py`.

        **Acceptance:**
        - 1.1.1 - Behavior is complete. file: `src/large.py`.
        - 1.1.2 - Helpers move to the new module. file: `src/large_helpers.py`.
        """,
        project_root=tmp_path,
    )

    assert not any(issue.code == "production-size-growth" for issue in result.issues)


def test_production_size_growth_excludes_test_module_outside_tests_directory(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "src" / "runner_tests.rs"
    source_path.parent.mkdir()
    source_path.write_text("fn case() {}\n" * 850, encoding="utf-8")

    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** production-size-test-module

        # Production Size Test Module

        ## P1: Work
        `kind: framing`

        ### 1.1 Extend test module [category: test]
        `kind: deliverable`

        Target: `src/runner_tests.rs::case`

        Update test coverage.

        **Acceptance:**
        - 1.1.1 - Test coverage is complete. test: `src/runner_tests.rs::case`.
        """,
        project_root=tmp_path,
    )

    assert not any(issue.code == "production-size-growth" for issue in result.issues)


def test_production_size_growth_accepts_split_verb_on_separate_body_line(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "src" / "large.py"
    source_path.parent.mkdir()
    source_path.write_text("value = 1\n" * 850, encoding="utf-8")

    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** production-size-wrapped-split

        # Production Size Wrapped Split

        ## P1: Work
        `kind: framing`

        ### 1.1 Split large module [category: code]
        `kind: deliverable`

        Targets:
        - `src/large.py::run`
        - `src/large_helpers.py`

        Split `src/large.py` across a smaller helper.
        The new file is `large_helpers.py`.

        **Acceptance:**
        - 1.1.1 - Behavior is complete. file: `src/large.py`.
        - 1.1.2 - Helpers move to the new module. file: `src/large_helpers.py`.
        """,
        project_root=tmp_path,
    )

    assert not any(issue.code == "production-size-growth" for issue in result.issues)


@pytest.mark.parametrize(
    ("trigger", "missing_carrier"),
    [
        (
            "crates/gcore/assets/schema/migrations/401_example.sql",
            "crates/gcore/assets/schema/catalog.manifest.json",
        ),
        (
            "src/gobby/config/example.py",
            "crates/gcore/assets/config/runtime_config_contract.json",
        ),
    ],
)
def test_derived_carriers_reports_static_trigger_table_omissions(
    tmp_path: Path,
    trigger: str,
    missing_carrier: str,
) -> None:
    result = _lint_plan_text(
        tmp_path,
        f"""
        > **Plan ID:** derived-carriers

        # Derived Carriers

        ## P1: Work
        `kind: framing`

        ### 1.1 Change source [category: code]
        `kind: deliverable`

        Target: `{trigger}`

        Implement the source change.

        **Acceptance:**
        - 1.1.1 - Source change is complete. file: `{trigger}`.
        """,
        project_root=tmp_path,
    )

    issues = [issue for issue in result.issues if issue.code == "derived-carriers"]
    assert len(issues) == 1
    assert missing_carrier in issues[0].message


def test_derived_carriers_accepts_carriers_in_dependent_deliverable(tmp_path: Path) -> None:
    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** derived-carriers-dependent

        # Derived Carriers In Dependent Deliverable

        ## P1: Work
        `kind: framing`

        ### 1.1 Add migration [category: code]
        `kind: deliverable`

        Target: `crates/gcore/assets/schema/migrations/401_example.sql`

        Implement the migration.

        **Acceptance:**
        - 1.1.1 - Migration is complete. file: `crates/gcore/assets/schema/migrations/401_example.sql`.

        ### 1.2 Regenerate carriers [category: code] (depends: 1.1)
        `kind: deliverable`

        Targets:
        - `crates/gcore/assets/schema/catalog.manifest.json`
        - `crates/gcore/src/grant/bundle.rs`
        - `crates/gcore/tests/schema_contract.rs`
        - `crates/gdaemon/tests/cli_contract.rs`
        - `src/gobby/storage/schema_expected_identity.json`

        Regenerate the derived carriers.

        **Acceptance:**
        - 1.2.1 - Catalog is regenerated. file: `crates/gcore/assets/schema/catalog.manifest.json`.
        - 1.2.2 - Grant bundle is regenerated. file: `crates/gcore/src/grant/bundle.rs`.
        - 1.2.3 - Schema contract is regenerated. file: `crates/gcore/tests/schema_contract.rs`.
        - 1.2.4 - CLI contract is regenerated. file: `crates/gdaemon/tests/cli_contract.rs`.
        - 1.2.5 - Expected identity is regenerated. file: `src/gobby/storage/schema_expected_identity.json`.
        """,
        project_root=tmp_path,
    )

    assert not any(issue.code == "derived-carriers" for issue in result.issues)


def test_production_size_growth_split_exemption_is_per_file(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "large_a.py").write_text("value = 1\n" * 850, encoding="utf-8")
    (source_dir / "large_b.py").write_text("value = 1\n" * 850, encoding="utf-8")

    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** production-size-per-file

        # Production Size Per File

        ## P1: Work
        `kind: framing`

        ### 1.1 Split one module, grow another [category: code]
        `kind: deliverable`

        Targets:
        - `src/large_a.py::run`
        - `src/large_a_helpers.py`
        - `src/large_b.py::run`

        Split `src/large_a.py` and move helpers into `src/large_a_helpers.py`.

        Extend `src/large_b.py` in place.

        **Acceptance:**
        - 1.1.1 - Behavior is complete. file: `src/large_a.py`.
        - 1.1.2 - Helpers move to the new module. file: `src/large_a_helpers.py`.
        - 1.1.3 - Other behavior is complete. file: `src/large_b.py`.
        """,
        project_root=tmp_path,
    )

    flagged = [
        issue.details["file_path"]
        for issue in result.issues
        if issue.code == "production-size-growth"
    ]
    assert flagged == ["src/large_b.py"]


def test_production_size_growth_split_paragraph_must_name_the_large_file(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "src" / "large.py"
    source_path.parent.mkdir()
    source_path.write_text("value = 1\n" * 850, encoding="utf-8")

    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** production-size-unanchored-split

        # Production Size Unanchored Split

        ## P1: Work
        `kind: framing`

        ### 1.1 Grow module [category: code]
        `kind: deliverable`

        Targets:
        - `src/large.py::run`
        - `src/large_helpers.py`

        Move the helper registry into `src/large_helpers.py`.

        Extend `src/large.py` with the new entry point.

        **Acceptance:**
        - 1.1.1 - Behavior is complete. file: `src/large.py`.
        - 1.1.2 - Registry lives in the helper module. file: `src/large_helpers.py`.
        """,
        project_root=tmp_path,
    )

    assert any(issue.code == "production-size-growth" for issue in result.issues)


def test_production_size_growth_rust_count_stops_at_test_module(tmp_path: Path) -> None:
    source_path = tmp_path / "crates" / "lib.rs"
    source_path.parent.mkdir()
    production = "fn f() {}\n" * 800
    tests = "#[cfg(test)]\nmod tests {\n" + "    #[test] fn t() {}\n" * 100 + "}\n"
    source_path.write_text(production + tests, encoding="utf-8")

    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** production-size-rust-tail

        # Production Size Rust Tail

        ## P1: Work
        `kind: framing`

        ### 1.1 Grow crate [category: code]
        `kind: deliverable`

        Target: `crates/lib.rs::f`

        Extend `crates/lib.rs`.

        **Acceptance:**
        - 1.1.1 - Behavior is complete. file: `crates/lib.rs`.
        """,
        project_root=tmp_path,
    )

    assert not any(issue.code == "production-size-growth" for issue in result.issues)


@pytest.mark.parametrize(
    ("header", "expected_flagged"),
    [
        ("# Regenerate this file with make generated-assets\n", True),
        ("# @generated by make\n", False),
        ("// DO NOT EDIT\n", False),
        ("# This file is auto-generated.\n", False),
        ("# Generated by protoc\n", False),
    ],
)
def test_production_size_growth_generated_marker_requires_explicit_phrase(
    tmp_path: Path,
    header: str,
    expected_flagged: bool,
) -> None:
    source_path = tmp_path / "src" / "large.py"
    source_path.parent.mkdir()
    source_path.write_text(header + "value = 1\n" * 850, encoding="utf-8")

    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** production-size-generated

        # Production Size Generated

        ## P1: Work
        `kind: framing`

        ### 1.1 Grow module [category: code]
        `kind: deliverable`

        Target: `src/large.py::run`

        Extend `src/large.py`.

        **Acceptance:**
        - 1.1.1 - Behavior is complete. file: `src/large.py`.
        """,
        project_root=tmp_path,
    )

    flagged = any(issue.code == "production-size-growth" for issue in result.issues)
    assert flagged is expected_flagged


def test_production_size_growth_warns_when_project_root_is_missing(tmp_path: Path) -> None:
    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** production-size-no-root

        # Production Size No Root

        ## P1: Work
        `kind: framing`

        ### 1.1 Grow module [category: code]
        `kind: deliverable`

        Target: `src/large.py::run`

        Extend `src/large.py`.

        **Acceptance:**
        - 1.1.1 - Behavior is complete. file: `src/large.py`.
        """,
    )

    assert result.valid
    assert result.warnings == ("production-size-growth skipped: no project root",)


def test_shared_target_ordering_ignores_scope_reason_paths(tmp_path: Path) -> None:
    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** shared-target-scope-reason

        # Shared Target Scope Reason

        ## P1: Work
        `kind: framing`

        ### 1.1 Rewrite helpers [category: code]
        `kind: deliverable`

        Target: `src/helpers.py::*` — scope-reason: mirror `src/shared.py` helpers

        Rewrite the helpers.

        **Acceptance:**
        - 1.1.1 - Helpers are rewritten. file: `src/helpers.py`.

        ### 1.2 Shared owner [category: code]
        `kind: deliverable`

        Target: `src/shared.py::second`

        Implement the shared owner.

        **Acceptance:**
        - 1.2.1 - Shared owner is complete. file: `src/shared.py`.
        """,
    )

    assert not any(issue.code == "shared-target-ordering" for issue in result.issues)


def test_derived_carriers_ignore_scope_reason_paths(tmp_path: Path) -> None:
    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** derived-carriers-scope-reason

        # Derived Carriers Scope Reason

        ## P1: Work
        `kind: framing`

        ### 1.1 Rewrite helpers [category: code]
        `kind: deliverable`

        Target: `src/helpers.py::*` — scope-reason: keep crates/gcore/assets/schema/baseline.sql in sync

        Rewrite the helpers.

        **Acceptance:**
        - 1.1.1 - Helpers are rewritten. file: `src/helpers.py`.
        """,
    )

    assert not any(issue.code == "derived-carriers" for issue in result.issues)


def test_shared_target_ordering_issue_carries_section_line(tmp_path: Path) -> None:
    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** shared-target-line

        # Shared Target Line

        ## P1: Work
        `kind: framing`

        ### 1.1 First owner [category: code]
        `kind: deliverable`

        Target: `src/shared.py::first`

        Implement the first owner.

        **Acceptance:**
        - 1.1.1 - First owner is complete. file: `src/shared.py`.

        ### 1.2 Second owner [category: code]
        `kind: deliverable`

        Target: `src/shared.py::second`

        Implement the second owner.

        **Acceptance:**
        - 1.2.1 - Second owner is complete. file: `src/shared.py`.
        """,
    )

    issues = [issue for issue in result.issues if issue.code == "shared-target-ordering"]
    assert len(issues) == 1
    assert issues[0].line is not None
    assert "line " in issues[0].to_error()


def test_unresolved_dependency_reference_is_an_error(tmp_path: Path) -> None:
    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** unresolved-dependency

        # Unresolved Dependency

        ## P1: Work
        `kind: framing`

        ### 1.1 Owner [category: code] (depends: 9.9, P7)
        `kind: deliverable`

        Target: `src/shared.py::first`

        Implement the owner.

        **Acceptance:**
        - 1.1.1 - Owner is complete. file: `src/shared.py`.
        """,
    )

    issues = [issue for issue in result.issues if issue.code == "unresolved-dependency"]
    assert [issue.details["reference"] for issue in issues] == ["9.9", "P7"]
    assert all(issue.section_id == "1.1" for issue in issues)
    assert all(issue.line is not None for issue in issues)


def test_phase_heading_dependency_applies_to_every_deliverable(tmp_path: Path) -> None:
    result = _lint_plan_text(
        tmp_path,
        """
        > **Plan ID:** phase-heading-dependency

        # Phase Heading Dependency

        ## P1: Foundation
        `kind: framing`

        ### 1.1 First owner [category: code]
        `kind: deliverable`

        Target: `src/shared.py::first`

        Implement the first owner.

        **Acceptance:**
        - 1.1.1 - First owner is complete. file: `src/shared.py`.

        ## P2: Follow-up (depends: P1)
        `kind: framing`

        ### 2.1 Second owner [category: code]
        `kind: deliverable`

        Target: `src/shared.py::second`

        Implement the second owner.

        **Acceptance:**
        - 2.1.1 - Second owner is complete. file: `src/shared.py`.
        """,
    )

    assert not any(issue.code == "shared-target-ordering" for issue in result.issues)
    assert not any(issue.code == "unresolved-dependency" for issue in result.issues)
