from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from gobby.cli.plans import _root_ref_from_file, plans

pytestmark = pytest.mark.unit
plans_module = importlib.import_module("gobby.cli.plans")


@dataclass(frozen=True)
class _Symbol:
    id: str
    name: str
    qualified_name: str
    file_path: str


class _FakeDb:
    def close(self) -> None:
        pass


class _FakeCodeIndexStorage:
    def get_project_stats(self, project_id: str) -> object | None:
        return object() if project_id == "project-1" else None

    def search_symbols_by_name(
        self,
        query: str,
        project_id: str,
        kind: str | None = None,
        file_path: str | None = None,
        limit: int = 50,
    ) -> tuple[_Symbol, ...]:
        del kind, file_path, limit
        if project_id != "project-1" or query != "app.service.do_work":
            return ()
        return (
            _Symbol(
                id="sym-do-work",
                name="do_work",
                qualified_name="app.service.do_work",
                file_path="src/service.py",
            ),
        )

    def find_direct_callers(
        self,
        project_id: str,
        symbol_ids: tuple[str, ...],
        callee_names: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        del callee_names
        if project_id == "project-1" and symbol_ids == ("sym-do-work",):
            return [{"file_path": "src/api.py"}, {"file_path": "tests/test_api.py"}]
        return []


def _write_contract_plan(tmp_path: Path, *, target_line: str = "Target: `docs/demo.md`") -> Path:
    path = tmp_path / "plan.md"
    path.write_text(
        f"""> **Plan ID:** cli-plan

# CLI Plan

## P1: Work
`kind: framing`

### 1.1 Work [category: docs]
`kind: deliverable`

{target_line}

Update docs/demo.md.

**Acceptance:**
- 1.1.1 - Docs exist. file: `docs/demo.md`
""",
        encoding="utf-8",
    )
    return path


def _write_consumer_plan(
    tmp_path: Path,
    *,
    target_line: str = "Target: `src/service.py`",
) -> Path:
    path = tmp_path / "consumer.md"
    path.write_text(
        f"""> **Plan ID:** cli-consumer-plan

# CLI Consumer Plan

## P1: Work
`kind: framing`

### 1.1 Rename Service [category: code]
`kind: deliverable`

{target_line}

Rename the service implementation.

**Acceptance:**
- 1.1.1 - Service file changes. file: `src/service.py`.
- 1.1.2 - Service symbol changes. symbol: `app.service.do_work`.
""",
        encoding="utf-8",
    )
    return path


def test_root_ref_from_file_reads_front_matter(tmp_path: Path) -> None:
    plan = tmp_path / "manual-plan.md"
    plan.write_text(
        """---
root_task_ref: "root-123"
---

# Planning Notes
""",
        encoding="utf-8",
    )

    assert _root_ref_from_file(plan) == "root-123"


def test_root_ref_from_file_reads_top_level_metadata(tmp_path: Path) -> None:
    plan = tmp_path / "manual-plan.md"
    plan.write_text(
        """root_task_ref: #123

# Planning Notes
""",
        encoding="utf-8",
    )

    assert _root_ref_from_file(plan) == "#123"


def test_root_ref_from_file_only_strips_matching_quote_pairs(tmp_path: Path) -> None:
    plan = tmp_path / "manual-plan.md"
    plan.write_text(
        """root_task_ref: '#123"

# Planning Notes
""",
        encoding="utf-8",
    )

    expected = "'#123" + '"'
    assert _root_ref_from_file(plan) == expected


def test_validate_command_runs_semantic_lint_and_skips_consumer_without_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _write_contract_plan(tmp_path)
    monkeypatch.setattr(plans_module, "resolve_project_ref", lambda project_ref: None)

    result = CliRunner().invoke(plans, ["validate", str(plan)])

    assert result.exit_code == 0
    assert "Plan:" in result.output
    assert "Consumer sweep: skipped (missing project_id)" in result.output


def test_validate_command_returns_semantic_lint_errors(tmp_path: Path) -> None:
    plan = _write_contract_plan(tmp_path, target_line="")

    result = CliRunner().invoke(plans, ["validate", str(plan)])

    assert result.exit_code != 0
    assert "target-coverage" in result.output


def test_validate_command_runs_consumer_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _write_consumer_plan(tmp_path)
    storage = _FakeCodeIndexStorage()
    monkeypatch.setattr(plans_module, "resolve_project_ref", lambda project_ref: "project-1")
    monkeypatch.setattr(plans_module, "_open_db", lambda: _FakeDb())
    monkeypatch.setattr(plans_module, "CodeIndexStorage", lambda db: storage)

    result = CliRunner().invoke(
        plans,
        ["validate", str(plan), "--project", "gobby", "--include-tests"],
    )

    assert result.exit_code != 0
    assert "consumer-sweep" in result.output
    assert "src/api.py" in result.output
    assert "tests/test_api.py" in result.output


def test_validate_command_expansion_mode_includes_test_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _write_consumer_plan(tmp_path)
    storage = _FakeCodeIndexStorage()
    monkeypatch.setattr(plans_module, "resolve_project_ref", lambda project_ref: "project-1")
    monkeypatch.setattr(plans_module, "_open_db", lambda: _FakeDb())
    monkeypatch.setattr(plans_module, "CodeIndexStorage", lambda db: storage)

    result = CliRunner().invoke(
        plans,
        ["validate", str(plan), "--project", "gobby", "--mode", "expansion"],
    )

    assert result.exit_code != 0
    assert "consumer-sweep" in result.output
    assert "tests/test_api.py" in result.output


def test_validate_command_excludes_test_consumers_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _write_consumer_plan(
        tmp_path,
        target_line="Targets:\n- `src/service.py`\n- `src/api.py`",
    )
    storage = _FakeCodeIndexStorage()
    monkeypatch.setattr(plans_module, "resolve_project_ref", lambda project_ref: "project-1")
    monkeypatch.setattr(plans_module, "_open_db", lambda: _FakeDb())
    monkeypatch.setattr(plans_module, "CodeIndexStorage", lambda db: storage)

    result = CliRunner().invoke(plans, ["validate", str(plan), "--project", "gobby"])

    assert result.exit_code == 0
    assert "Consumer sweep: passed" in result.output
    assert "tests/test_api.py" not in result.output


def test_validate_command_handles_missing_phase_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _write_contract_plan(tmp_path)
    monkeypatch.setattr(
        plans_module,
        "_validate_plan_for_cli",
        lambda *_args, **_kwargs: {
            "valid": True,
            "path": str(plan),
            "consumer_sweep": {"valid": True, "skipped": True, "skip_reason": "missing project_id"},
        },
    )

    result = CliRunner().invoke(plans, ["validate", str(plan)])

    assert result.exit_code == 0
    assert "Phases: 0" in result.output
    assert "No phase metadata available" in result.output


def test_validate_cli_reports_consumer_sweep_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _write_contract_plan(tmp_path)
    monkeypatch.setattr(plans_module, "resolve_project_ref", lambda project_ref: "project-1")
    monkeypatch.setattr(plans_module, "_open_db", lambda: _FakeDb())
    monkeypatch.setattr(plans_module, "CodeIndexStorage", lambda db: _FakeCodeIndexStorage())
    monkeypatch.setattr(
        plans_module,
        "run_consumer_sweep",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("index unavailable")),
    )

    result = plans_module._validate_plan_for_cli(plan, "gobby", include_tests=False)

    assert result["valid"] is False
    assert result["errors"] == ["Consumer sweep failed: index unavailable"]
