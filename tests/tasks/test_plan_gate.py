"""Tests for the spawn-time plan validation gate.

Specifically: ``planner``, ``plan-adversary``, and ``plan-enhancer`` spawns
refuse to start when the task's ``plan_file_path`` artifact fails the
Plan-Coverage Contract validator. Other agents pass through, and the gate is a
no-op when no plan artifact is recorded.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest

from gobby.tasks.expansion import _plan_gate as plan_gate_module
from gobby.tasks.expansion._plan_gate import (
    PLANNING_AGENTS,
    validate_plan_for_agent_spawn,
)
from tests.agents.prepared_spawn import prepared_spawn
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal

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


def _write_missing_plan_id(path: Path) -> Path:
    """Canonical implementation plan missing its required Plan ID marker."""
    path.write_text(
        """# Missing Plan ID

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


def _make_task_manager_with_artifact(plan_file_path: str | None) -> MagicMock:
    artifacts = MagicMock()
    artifacts.plan_file_path = plan_file_path
    manager = MagicMock()
    manager.get_artifacts = MagicMock(return_value=artifacts)
    manager.get_task = MagicMock(return_value=SimpleNamespace(project_id="project-1"))
    return manager


class _FreshZeroSymbolIndex:
    def __init__(self, source_path: Path) -> None:
        self.source_path = source_path

    def get_project_stats(self, project_id: str) -> object:
        assert project_id == "project-1"
        return object()

    def get_file(self, project_id: str, file_path: str) -> SimpleNamespace:
        assert project_id == "project-1"
        assert file_path == "src/foo.py"
        content_hash = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        return SimpleNamespace(content_hash=content_hash, symbol_count=0)

    def get_symbols_for_file(self, project_id: str, file_path: str) -> list[object]:
        assert project_id == "project-1"
        assert file_path == "src/foo.py"
        return []


def test_planning_agents_constant() -> None:
    assert PLANNING_AGENTS == frozenset({"planner", "plan-adversary", "plan-enhancer"})


def test_plan_enhancer_spawn_against_malformed_plan_returns_structured_failure(
    tmp_path: Path,
) -> None:
    plan = _write_broken_plan(tmp_path / "broken.md")
    manager = _make_task_manager_with_artifact(str(plan))

    result = validate_plan_for_agent_spawn(
        agent_name="plan-enhancer", task_id="t1", task_manager=manager
    )

    assert result is not None
    assert result["success"] is False
    assert "PlanValidationError" in result["error"]


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


@pytest.mark.parametrize("agent_name", ["planner", "plan-enhancer"])
def test_repair_agent_spawn_receives_missing_index_diagnostics(
    tmp_path: Path,
    agent_name: str,
) -> None:
    plan = _write_clean_plan(tmp_path / "clean.md")
    manager = _make_task_manager_with_artifact(str(plan))

    result = validate_plan_for_agent_spawn(
        agent_name=agent_name,
        task_id="t1",
        task_manager=manager,
    )

    assert result is not None
    assert result["success"] is True
    assert "prompt_append" in result
    assert result["symbol_validation"]["status"] == "failed"


def test_planner_spawn_against_fresh_index_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _write_clean_plan(tmp_path / "clean.md")
    source_path = tmp_path / "src" / "foo.py"
    source_path.parent.mkdir()
    source_path.write_text("", encoding="utf-8")
    manager = _make_task_manager_with_artifact(str(plan))
    monkeypatch.setattr(
        plan_gate_module,
        "get_project_context",
        lambda _path: {"id": "project-1", "project_path": str(tmp_path)},
    )

    result = validate_plan_for_agent_spawn(
        agent_name="planner",
        task_id="t1",
        task_manager=manager,
        code_index=_FreshZeroSymbolIndex(source_path),
    )

    assert result is None


def test_plan_adversary_blocks_when_symbol_validation_cannot_run(tmp_path: Path) -> None:
    plan = _write_clean_plan(tmp_path / "clean.md")
    manager = _make_task_manager_with_artifact(str(plan))

    result = validate_plan_for_agent_spawn(
        agent_name="plan-adversary",
        task_id="t1",
        task_manager=manager,
    )

    assert result is not None
    assert result["success"] is False
    assert result["symbol_validation"]["status"] == "failed"


@pytest.mark.parametrize("agent_name", ("planner", "plan-adversary"))
def test_planning_agent_spawn_rejects_missing_plan_id(tmp_path: Path, agent_name: str) -> None:
    plan = _write_missing_plan_id(tmp_path / "missing-id.md")
    manager = _make_task_manager_with_artifact(str(plan))

    result = validate_plan_for_agent_spawn(
        agent_name=agent_name, task_id="t1", task_manager=manager
    )

    assert result is not None
    assert result["success"] is False
    assert result["error"].startswith("PlanValidationError:")
    assert result["validator_errors"] == result["validator_warnings"]
    assert "real **Plan ID:**" in result["validator_warnings"][0]
    assert "covers:unknown:*" in result["validator_warnings"][0]


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


def test_transient_postgres_error_skips_plan_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    plan = _write_clean_plan(tmp_path / "clean.md")
    manager = _make_task_manager_with_artifact(str(plan))

    def fail_validation(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise psycopg.OperationalError("database temporarily unavailable")

    monkeypatch.setattr(
        "gobby.tasks.expansion._validate.validate_plan_file",
        fail_validation,
    )
    caplog.set_level(logging.WARNING)

    result = validate_plan_for_agent_spawn(
        agent_name="planner",
        task_id="t1",
        task_manager=manager,
    )

    assert result is None
    assert "Skipping plan validation gate" in caplog.text
    assert "database temporarily unavailable" in caplog.text


@pytest.mark.asyncio
async def test_spawn_agent_impl_dispatches_plan_gate_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
    from gobby.tasks.expansion import _plan_gate as plan_gate_module

    caller_thread = threading.get_ident()
    gate_threads: list[int] = []
    gate_failure = {"success": False, "error": "PlanValidationError: blocked"}

    def gate(*_args: object, **_kwargs: object) -> dict[str, object]:
        gate_threads.append(threading.get_ident())
        return gate_failure

    monkeypatch.setattr(plan_gate_module, "validate_plan_for_agent_spawn", gate)

    result = await spawn_agent_impl(
        prompt="review plan",
        runner=MagicMock(),
        agent_lookup_name="planner",
        task_id="t1",
        task_manager=MagicMock(),
    )

    assert result == gate_failure
    assert len(gate_threads) == 1
    assert gate_threads[0] != caller_thread


@pytest.mark.asyncio
async def test_spawn_agent_impl_injects_symbol_repair_diagnostics(tmp_path: Path) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "ok", 0)
    runner.child_session_manager = MagicMock()
    runner.run_storage = MagicMock()
    runner.run_storage.has_active_run_for_task.return_value = False
    daemon_config = SimpleNamespace(
        database_url="postgresql://user:pass@127.0.0.1/gobby",
        bind_host="127.0.0.1",
        daemon_port=60887,
    )
    spawn_result = SimpleNamespace(
        success=True,
        child_session_id="child-1",
        status="running",
        terminal_type="process",
        terminal_id=None,
        tmux_socket_name=None,
        tmux_socket_path=None,
        pid=123,
        message="spawned",
    )
    gate_result = {
        "success": True,
        "prompt_append": "Symbol validation diagnostics:\n- [target_symbol_required] Fix scope",
    }

    with (
        patch(
            "gobby.tasks.expansion._plan_gate.validate_plan_for_agent_spawn",
            return_value=gate_result,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "project-1", "project_path": str(repo_path)},
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn",
            return_value=prepared_spawn(
                parent_session_id="parent-1",
                project_id="project-1",
            ),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            new=AsyncMock(return_value=spawn_result),
        ) as execute,
    ):
        result = await spawn_agent_impl(
            prompt="Repair the plan",
            runner=runner,
            agent_lookup_name="planner",
            parent_session_id="parent-1",
            provider="codex",
            isolation="none",
            daemon_config=daemon_config,
        )

    assert result["success"] is True, result
    await_args = execute.await_args
    assert await_args is not None
    spawn_request = await_args.args[0]
    assert spawn_request.prompt == (
        "Repair the plan\n\nSymbol validation diagnostics:\n- [target_symbol_required] Fix scope"
    )
