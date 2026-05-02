"""Tests for deterministic plan semantic lint."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from gobby.plans.parser import parse_plan
from gobby.plans.semantic_lint import lint_plan_document

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


def test_missing_acceptance_file_and_test_paths_fail(tmp_path: Path) -> None:
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

    assert any("tests/test_app.py" in error for error in errors)


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
