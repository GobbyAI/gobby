"""Tests for code-index-backed plan consumer sweep."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gobby.plans.consumer_sweep import run_consumer_sweep
from gobby.plans.parser import parse_plan

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Symbol:
    id: str
    name: str
    qualified_name: str
    file_path: str


class _Storage:
    def __init__(self, *, indexed: bool = True) -> None:
        self.indexed = indexed
        self.symbols = {
            "app.service.do_work": (
                _Symbol(
                    id="sym-do-work",
                    name="do_work",
                    qualified_name="app.service.do_work",
                    file_path="src/service.py",
                ),
            )
        }
        self.callers: dict[str, tuple[str, ...]] = {"sym-do-work": ("src/api.py",)}
        self.depth_two_callers: dict[str, tuple[str, ...]] = {
            "sym-do-work": ("src/cli.py",),
        }

    def get_project_stats(self, project_id: str) -> object | None:
        return object() if self.indexed and project_id == "project-1" else None

    def search_symbols_by_name(
        self,
        query: str,
        project_id: str,
        kind: str | None = None,
        file_path: str | None = None,
        limit: int = 50,
    ) -> tuple[_Symbol, ...]:
        del kind, file_path, limit
        if project_id != "project-1":
            return ()
        return self.symbols.get(query, ())

    def find_direct_callers(
        self,
        project_id: str,
        symbol_ids: tuple[str, ...],
        callee_names: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        del callee_names
        if project_id != "project-1":
            return []
        return [
            {"file_path": caller}
            for symbol_id in symbol_ids
            for caller in self.callers.get(symbol_id, ())
        ]


@dataclass(frozen=True)
class _CodeIndex:
    storage: _Storage
    graph: object = object()


def _write_plan(tmp_path: Path, targets: str) -> Path:
    path = tmp_path / "plan.md"
    header = textwrap.dedent(
        """
        > **Plan ID:** consumer-sweep

        # Consumer Sweep

        ## P1: Work
        `kind: framing`

        ### 1.1 Rename Service [category: code]
        `kind: deliverable`

        Targets:
        """
    ).lstrip()
    body = textwrap.dedent(
        """

        Rename symbol: `app.service.do_work` and update the implementation.

        **Acceptance:**
        - 1.1.1 - Service symbol is renamed. symbol: `app.service.do_work`.
        - 1.1.2 - Service file changes. file: `src/service.py`.
        """
    )
    path.write_text(
        header + textwrap.indent(targets, "        ") + body,
        encoding="utf-8",
    )
    return path


def test_destructive_symbol_change_with_unlisted_direct_caller_fails(tmp_path: Path) -> None:
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(_Storage()),
    )

    assert result.valid is False
    assert any("src/api.py" in error for error in result.errors)


def test_listed_direct_caller_passes(tmp_path: Path) -> None:
    plan = parse_plan(
        _write_plan(tmp_path, "- `src/service.py`\n- `src/api.py`"),
        parse_mode="draft",
    )

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(_Storage()),
    )

    assert result.valid is True
    assert result.skipped is False


def test_missing_index_skips_without_failing(tmp_path: Path) -> None:
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    result = run_consumer_sweep(plan, project_id="project-1", code_index=None)

    assert result.valid is True
    assert result.skipped is True


def test_unavailable_index_skips_without_failing(tmp_path: Path) -> None:
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(_Storage(indexed=False)),
    )

    assert result.valid is True
    assert result.skipped is True


def test_depth_two_only_consumer_does_not_fail_v1(tmp_path: Path) -> None:
    storage = _Storage()
    storage.callers = {"sym-do-work": ()}
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )

    assert result.valid is True
