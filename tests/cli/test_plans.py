from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from gobby.cli.plans import _root_ref_from_file, plans
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

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


class _NonClosingDb:
    def __init__(self, db: HubDatabase) -> None:
        self._db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

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


def _write_contract_plan_without_plan_id(tmp_path: Path) -> Path:
    path = tmp_path / "missing-id.md"
    path.write_text(
        """# CLI Plan

## P1: Work
`kind: framing`

### 1.1 Work [category: docs]
`kind: deliverable`

Target: `docs/demo.md`

Update docs/demo.md.

**Acceptance:**
- 1.1.1 - Docs exist. file: `docs/demo.md`
""",
        encoding="utf-8",
    )
    return path


def _write_register_plan(
    root: Path,
    *,
    name: str = "cli-register-plan",
    root_task_ref: str = "#100",
) -> Path:
    path = root / ".gobby" / "plans" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
root_task_ref: "{root_task_ref}"
---
> **Plan ID:** {name}

## P1 Phase 1
`kind: framing`

### 1.1 Register Plan [category: code]
`kind: deliverable`

Register the plan.

**Acceptance:**
- 1.1.1 - Plan row exists. file: `src/gobby/cli/plans.py`
""",
        encoding="utf-8",
    )
    return path


def _write_malformed_register_plan(root: Path) -> Path:
    path = root / ".gobby" / "plans" / "cli-malformed-plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
root_task_ref: "#100"
---
> **Plan ID:** cli-malformed-plan

## P1 Phase 1
`kind: framing`

### 1.1 Missing Kind [category: code]

This deliverable is malformed.
""",
        encoding="utf-8",
    )
    return path


def _write_binary_register_plan(root: Path) -> Path:
    path = root / ".gobby" / "plans" / "cli-binary-plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00")
    return path


def _create_project(temp_db: HubDatabase, root: Path) -> str:
    return LocalProjectManager(temp_db).create(name=f"plans-{root.name}", repo_path=str(root)).id


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


def test_register_command_writes_plan_row(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(temp_db, tmp_path)
    root_task = LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title="Plan root",
        category="planning",
        validation_criteria="The registered plan row references this planning root.",
    )
    root_task_ref = f"#{root_task.seq_num}"
    plan = _write_register_plan(tmp_path, root_task_ref=root_task_ref)
    monkeypatch.setattr(plans_module, "resolve_project_ref", lambda *_args, **_kwargs: project_id)
    monkeypatch.setattr(plans_module, "_open_db", lambda: _NonClosingDb(temp_db))

    result = CliRunner().invoke(plans, ["register", str(plan), "--project", "gobby"])

    assert result.exit_code == 0, result.output
    assert "Registered cli-register-plan (active)" in result.output
    record = LocalPlanManager(temp_db).get_plan("cli-register-plan", project_id=project_id)
    assert record.project_id == project_id
    assert record.plan_id == "cli-register-plan"
    assert record.plan_path == ".gobby/plans/cli-register-plan.md"
    assert record.plan_kind == "implementation"
    assert record.root_task_ref == root_task_ref
    assert record.state == "active"


def test_register_command_malformed_plan_raises_click_exception(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(temp_db, tmp_path)
    plan = _write_malformed_register_plan(tmp_path)
    monkeypatch.setattr(plans_module, "resolve_project_ref", lambda *_args, **_kwargs: project_id)
    monkeypatch.setattr(plans_module, "_open_db", lambda: _NonClosingDb(temp_db))

    with pytest.raises(click.ClickException) as exc_info:
        plans_module.register_plan_command.callback(
            plan_path=plan,
            plan_id=None,
            plan_kind="implementation",
            root_task_ref=None,
            project="gobby",
        )

    assert str(plan) in exc_info.value.message
    assert "missing kind: front-matter" in exc_info.value.message


def test_register_command_binary_plan_raises_click_exception(tmp_path: Path) -> None:
    plan = _write_binary_register_plan(tmp_path)

    with pytest.raises(click.ClickException) as exc_info:
        plans_module.register_plan_command.callback(
            plan_path=plan,
            plan_id="cli-binary-plan",
            plan_kind="implementation",
            root_task_ref=None,
            project="gobby",
        )

    assert "utf-8" in exc_info.value.message
    assert "Traceback" not in exc_info.value.message


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


def test_validate_command_reports_missing_plan_id_warning(tmp_path: Path) -> None:
    plan = _write_contract_plan_without_plan_id(tmp_path)

    result = CliRunner().invoke(plans, ["validate", str(plan)])

    assert result.exit_code != 0
    assert "Error: implementation plans must declare a real **Plan ID:**" in result.output
    assert "Warning: implementation plans must declare a real **Plan ID:**" in result.output
    assert "covers:unknown:*" in result.output


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
