from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
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


class _FakeIndex:
    def __init__(self, root: Path, *, available: bool = True) -> None:
        self.root = root
        self.available = available

    def get_project_stats(self, project_id: str) -> object | None:
        assert project_id == "project-1"
        return object() if self.available else None

    def get_file(self, project_id: str, file_path: str) -> SimpleNamespace | None:
        assert project_id == "project-1"
        assert file_path == "docs/demo.md"
        source_path = self.root / file_path
        if not source_path.exists():
            return None
        content_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        return SimpleNamespace(content_hash=content_hash, symbol_count=0)

    def get_symbols_for_file(self, project_id: str, file_path: str) -> list[_Symbol]:
        assert project_id == "project-1"
        assert file_path == "docs/demo.md"
        return []


class _NonClosingDb:
    def __init__(self, db: HubDatabase) -> None:
        self._db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    def close(self) -> None:
        pass


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


def test_register_command_writes_plan_row(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(temp_db, tmp_path)
    root_task = LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title="Plan root",
        category="planning",
        validation_criteria="The registered plan remains linked to this root task.",
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


def test_register_command_binary_plan_raises_click_exception(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(temp_db, tmp_path)
    plan = _write_binary_register_plan(tmp_path)
    monkeypatch.setattr(plans_module, "resolve_project_ref", lambda *_args, **_kwargs: project_id)
    monkeypatch.setattr(plans_module, "_open_db", lambda: _NonClosingDb(temp_db))

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


def test_validate_command_runs_semantic_lint_without_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _write_contract_plan(tmp_path)
    monkeypatch.setattr(plans_module, "resolve_project_ref", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(plans, ["validate", str(plan)])

    assert result.exit_code == 0
    assert "Plan:" in result.output
    assert "Phases: 1" in result.output
    assert "Symbol validation skipped" in result.output


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


def test_validate_helper_uses_auto_resolved_fresh_project_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _write_contract_plan(tmp_path)
    docs_path = tmp_path / "docs" / "demo.md"
    docs_path.parent.mkdir()
    docs_path.write_text("demo\n", encoding="utf-8")
    monkeypatch.setattr(
        plans_module,
        "resolve_project_ref",
        lambda *_args, **_kwargs: "project-1",
    )
    monkeypatch.setattr(plans_module, "_open_db", lambda: _FakeDb())
    monkeypatch.setattr(
        plans_module,
        "LocalProjectManager",
        lambda _db: SimpleNamespace(
            get=lambda _project_id: SimpleNamespace(repo_path=str(tmp_path))
        ),
    )
    monkeypatch.setattr(
        plans_module,
        "CodeIndexStorage",
        lambda _db: _FakeIndex(tmp_path),
    )

    result = plans_module._validate_plan_for_cli(plan, None, mode="standard")

    assert result["valid"] is True
    assert result["symbol_validation"]["status"] == "passed"
    assert result["symbol_validation"]["checked_targets"] == ["docs/demo.md"]


def test_validate_helper_explicit_project_fails_closed_without_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _write_contract_plan(tmp_path)
    monkeypatch.setattr(
        plans_module,
        "resolve_project_ref",
        lambda *_args, **_kwargs: "project-1",
    )
    monkeypatch.setattr(plans_module, "_open_db", lambda: _FakeDb())
    monkeypatch.setattr(
        plans_module,
        "LocalProjectManager",
        lambda _db: SimpleNamespace(
            get=lambda _project_id: SimpleNamespace(repo_path=str(tmp_path))
        ),
    )
    monkeypatch.setattr(
        plans_module,
        "CodeIndexStorage",
        lambda _db: _FakeIndex(tmp_path, available=False),
    )

    result = plans_module._validate_plan_for_cli(plan, "gobby", mode="standard")

    assert result["valid"] is False
    assert result["symbol_validation"]["status"] == "failed"
    assert result["symbol_validation"]["issues"][0]["code"] == "symbol_index_unavailable"


def test_validate_helper_expansion_mode_fails_closed_without_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _write_contract_plan(tmp_path)
    monkeypatch.setattr(plans_module, "resolve_project_ref", lambda *_args, **_kwargs: None)

    result = plans_module._validate_plan_for_cli(plan, None, mode="expansion")

    assert result["valid"] is False
    assert result["symbol_validation"]["status"] == "failed"
    assert result["symbol_validation"]["issues"][0]["code"] == "symbol_index_unavailable"
