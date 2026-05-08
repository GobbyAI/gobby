"""Tests for the spawn-time plan validation gate.

Specifically: ``planner`` and ``plan-adversary`` spawns refuse to start when
the task's ``plan_file_path`` artifact fails the Plan-Coverage Contract
validator. Other agents pass through, and the gate is a no-op when no plan
artifact is recorded.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.tasks.expansion._plan_gate import (
    PLANNING_AGENTS,
    validate_plan_for_agent_spawn,
)

pytestmark = pytest.mark.unit


def _write_broken_plan(path: Path) -> Path:
    """Phase headings use the pre-contract `## Phase N` form — silently dropped."""
    path.write_text(
        """> **Plan ID:** broken

# Broken

## Phase 1: Setup
`kind: framing`

### 1.1 Foundation [category: code]
`kind: deliverable`

Target: `src/foo.py`

Implement.

**Acceptance:**
- 1.1.1 - Done. file: `src/foo.py`
""",
        encoding="utf-8",
    )
    return path


def _write_clean_plan(path: Path) -> Path:
    """Canonical `## P<N>` form."""
    path.write_text(
        """> **Plan ID:** clean

# Clean

## P1: Setup
`kind: framing`

### 1.1 Foundation [category: code]
`kind: deliverable`

Target: `src/foo.py`

Implement.

**Acceptance:**
- 1.1.1 - Done. file: `src/foo.py`
""",
        encoding="utf-8",
    )
    return path


def _write_symbol_change_plan(path: Path, *, targets: str = "Target: `src/service.py`") -> Path:
    path.write_text(
        f"""> **Plan ID:** symbol-change

# Symbol Change

## P1: Setup
`kind: framing`

### 1.1 Rename Service [category: code]
`kind: deliverable`

{targets}

Rename symbol: `app.service.do_work`.

**Acceptance:**
- 1.1.1 - Service symbol changes. symbol: `app.service.do_work`.
- 1.1.2 - Service file changes. file: `src/service.py`.
""",
        encoding="utf-8",
    )
    return path


class _IndexedStorage:
    def get_project_stats(self, project_id: str) -> object | None:
        return object() if project_id == "project-1" else None

    def search_symbols_by_name(
        self,
        query: str,
        project_id: str,
        kind: str | None = None,
        file_path: str | None = None,
        limit: int = 50,
    ) -> tuple[SimpleNamespace, ...]:
        del kind, file_path, limit
        if project_id != "project-1" or query != "app.service.do_work":
            return ()
        return (
            SimpleNamespace(
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
    ) -> list[dict[str, str]]:
        del callee_names
        if project_id == "project-1" and "sym-do-work" in symbol_ids:
            return [{"file_path": "src/api.py"}, {"file_path": "tests/test_api.py"}]
        return []


def _make_task_manager_with_artifact(plan_file_path: str | None) -> MagicMock:
    artifacts = MagicMock()
    artifacts.plan_file_path = plan_file_path
    manager = MagicMock()
    manager.get_artifacts = MagicMock(return_value=artifacts)
    manager.get_task = MagicMock(return_value=SimpleNamespace(project_id="project-1"))
    return manager


def test_planning_agents_constant() -> None:
    assert PLANNING_AGENTS == frozenset({"planner", "plan-adversary"})


def test_non_planning_agent_passes_through() -> None:
    manager = _make_task_manager_with_artifact("/tmp/whatever.md")
    result = validate_plan_for_agent_spawn(
        agent_name="developer", task_id="t1", task_manager=manager
    )
    assert result is None
    manager.get_artifacts.assert_not_called()


def test_no_task_id_passes_through() -> None:
    manager = _make_task_manager_with_artifact("/tmp/whatever.md")
    result = validate_plan_for_agent_spawn(agent_name="planner", task_id=None, task_manager=manager)
    assert result is None


def test_no_task_manager_passes_through() -> None:
    result = validate_plan_for_agent_spawn(agent_name="planner", task_id="t1", task_manager=None)
    assert result is None


def test_no_plan_artifact_passes_through() -> None:
    manager = _make_task_manager_with_artifact(None)
    result = validate_plan_for_agent_spawn(
        agent_name="plan-adversary", task_id="t1", task_manager=manager
    )
    assert result is None


def test_planner_spawn_against_clean_plan_succeeds(tmp_path: Path) -> None:
    plan = _write_clean_plan(tmp_path / "clean.md")
    manager = _make_task_manager_with_artifact(str(plan))

    result = validate_plan_for_agent_spawn(agent_name="planner", task_id="t1", task_manager=manager)

    assert result is None


def test_planner_spawn_against_malformed_plan_returns_structured_failure(
    tmp_path: Path,
) -> None:
    plan = _write_broken_plan(tmp_path / "broken.md")
    manager = _make_task_manager_with_artifact(str(plan))

    result = validate_plan_for_agent_spawn(agent_name="planner", task_id="t1", task_manager=manager)

    assert result is not None
    assert result["success"] is False
    assert result["error"].startswith("PlanValidationError:")
    assert result["plan_file_path"] == str(plan)
    assert isinstance(result["validator_errors"], list)
    assert any("phase sections" in err for err in result["validator_errors"])


def test_plan_adversary_spawn_against_malformed_plan_returns_structured_failure(
    tmp_path: Path,
) -> None:
    plan = _write_broken_plan(tmp_path / "broken.md")
    manager = _make_task_manager_with_artifact(str(plan))

    result = validate_plan_for_agent_spawn(
        agent_name="plan-adversary", task_id="t1", task_manager=manager
    )

    assert result is not None
    assert result["success"] is False
    assert "PlanValidationError" in result["error"]


def test_missing_plan_file_returns_structured_failure(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.md"
    manager = _make_task_manager_with_artifact(str(missing))

    result = validate_plan_for_agent_spawn(agent_name="planner", task_id="t1", task_manager=manager)

    assert result is not None
    assert result["success"] is False
    assert "PlanValidationError" in result["error"]


def test_artifact_lookup_failure_passes_through(tmp_path: Path) -> None:
    """If get_artifacts raises, do not block the spawn — fail open. The
    underlying validator stays defensively strict for plans we can read."""
    manager = MagicMock()
    manager.get_artifacts = MagicMock(side_effect=RuntimeError("db error"))

    result = validate_plan_for_agent_spawn(agent_name="planner", task_id="t1", task_manager=manager)

    assert result is None


def test_plan_adversary_spawn_blocks_on_consumer_sweep(tmp_path: Path) -> None:
    plan = _write_symbol_change_plan(tmp_path / "symbol.md")
    manager = _make_task_manager_with_artifact(str(plan))
    code_index = SimpleNamespace(storage=_IndexedStorage(), graph=object())

    result = validate_plan_for_agent_spawn(
        agent_name="plan-adversary",
        task_id="t1",
        task_manager=manager,
        code_index=code_index,
    )

    assert result is not None
    assert result["success"] is False
    assert "consumer-sweep" in result["error"]
    assert "src/api.py" in result["error"]
    assert "tests/test_api.py" not in result["error"]
    assert result["consumer_sweep"]["valid"] is False


def test_plan_adversary_spawn_excludes_test_consumers(tmp_path: Path) -> None:
    plan = _write_symbol_change_plan(
        tmp_path / "symbol.md",
        targets="Targets:\n- `src/service.py`\n- `src/api.py`",
    )
    manager = _make_task_manager_with_artifact(str(plan))
    code_index = SimpleNamespace(storage=_IndexedStorage(), graph=object())

    result = validate_plan_for_agent_spawn(
        agent_name="plan-adversary",
        task_id="t1",
        task_manager=manager,
        code_index=code_index,
    )

    assert result is None


def test_draft_parse_failure_logs_and_skips_consumer_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.plans.parser import PlanParseError

    plan = _write_clean_plan(tmp_path / "clean.md")
    manager = _make_task_manager_with_artifact(str(plan))
    monkeypatch.setattr(
        "gobby.tasks.expansion._validate.validate_plan_file",
        lambda *_args, **_kwargs: {"valid": True},
    )
    monkeypatch.setattr(
        "gobby.plans.parser.parse_plan",
        lambda path, **_kwargs: (_ for _ in ()).throw(
            PlanParseError([(1, "bad draft")], Path(path))
        ),
    )

    caplog.set_level(logging.WARNING)

    result = validate_plan_for_agent_spawn(
        agent_name="planner",
        task_id="t1",
        task_manager=manager,
        code_index=object(),
    )

    assert result is None
    assert "draft plan parse failed" in caplog.text
