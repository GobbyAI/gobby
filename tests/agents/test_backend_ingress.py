"""Plan 4.2: terminal_backend at every spawn ingress and on SpawnRequest."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from gobby.agents.spawn_models import SpawnRequest
from gobby.config.terminals import TerminalConfig
from gobby.dispatch._planning_enhancement import _spawn_plan_enhancer
from gobby.dispatch._rule_actions import _spawn_stage_agent
from gobby.dispatch.actions import SpawnAgentAction
from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
from gobby.servers.routes.agent_spawn import AgentSpawnRequest, BatchSpawnRequest
from tests.agents.prepared_spawn import prepared_spawn

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SPAWN_REQUEST_SCAN_PATHS = (
    ROOT / "src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py",
    ROOT / "tests/agents/test_spawn_executor.py",
    ROOT / "tests/agents/test_spawn_executor_droid.py",
    ROOT / "tests/agents/test_srt_spawn.py",
    ROOT / "tests/agents/test_verified_review_regressions.py",
    ROOT / "tests/mcp_proxy/tools/test_spawn_agent_speed.py",
)


def _keyword_names(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg is not None}


def _is_spawn_request_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name) and func.id == "SpawnRequest":
        return True
    return isinstance(func, ast.Attribute) and func.attr == "SpawnRequest"


def _dict_literal_keys(node: ast.expr) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    keys: set[str] = set()
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
    return keys


def spawn_request_constructions_missing_backend(path: Path) -> list[int]:
    """Return 1-based lines of SpawnRequest(...) that omit terminal_backend."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_spawn_request_call(node):
            continue
        names = _keyword_names(node)
        if "terminal_backend" in names:
            continue
        if any(kw.arg is None for kw in node.keywords):
            star = next(kw.value for kw in node.keywords if kw.arg is None)
            if isinstance(star, ast.Name):
                assigned = _assigned_dict_keys(tree, star.id)
                if "terminal_backend" in assigned:
                    continue
        missing.append(getattr(node, "lineno", 0))
    return missing


def _assigned_dict_keys(tree: ast.AST, name: str) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if name in targets:
                keys.update(_dict_literal_keys(node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                keys.update(_dict_literal_keys(node.value))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"update", "setdefault"} and isinstance(node.func.value, ast.Name):
                if node.func.value.id == name and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and arg.value == "terminal_backend":
                        keys.add("terminal_backend")
                    keys.update(_dict_literal_keys(arg))
    return keys


def test_backend_field_at_every_spawn_ingress() -> None:
    registry = create_spawn_agent_registry(MagicMock(), db=MagicMock())
    schema = registry.get_schema("spawn_agent")
    assert schema is not None
    properties = schema["inputSchema"]["properties"]
    assert "terminal_backend" in properties
    backend_schema = properties["terminal_backend"]
    allowed = backend_schema.get("enum") or backend_schema.get("anyOf")
    if isinstance(allowed, list) and allowed and isinstance(allowed[0], str):
        assert set(allowed) == {"tmux", "native"}

    defaulted = AgentSpawnRequest(task_id="#1")
    native = AgentSpawnRequest(task_id="#1", terminal_backend="native")
    assert defaulted.terminal_backend in {None, "tmux"}
    assert native.terminal_backend == "native"
    with pytest.raises(ValidationError):
        AgentSpawnRequest(task_id="#1", terminal_backend="ghostty")
    batch = BatchSpawnRequest(spawns=[AgentSpawnRequest(task_id="#1", terminal_backend="native")])
    assert batch.spawns[0].terminal_backend == "native"
    with pytest.raises(ValidationError):
        BatchSpawnRequest(spawns=[AgentSpawnRequest(task_id="#1", terminal_backend="pty")])

    native_action = SpawnAgentAction(
        task_id="7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
        task_ref="#1",
        agent_slug="backend-developer",
        prompt="go",
        terminal_backend="native",
    )
    assert native_action.terminal_backend == "native"
    with pytest.raises(ValueError, match="terminal_backend"):
        SpawnAgentAction(
            task_id="7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
            task_ref="#1",
            agent_slug="backend-developer",
            prompt="go",
            terminal_backend=cast(Literal["tmux", "native"], "ghostty"),
        )

    stage = SimpleNamespace(name="planning", stage_name="planning", state="ready", position=0)
    task = SimpleNamespace(
        id="7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
        ref="#1",
        additional_skills=(),
    )
    context = SimpleNamespace(prompt_context={})
    rule_action = _spawn_stage_agent(task, stage, context, "planner")
    assert rule_action.terminal_backend in {"tmux", "native"}
    enhancement = _spawn_plan_enhancer(task, stage, context, round_number=1, max_rounds=2)
    assert enhancement.terminal_backend in {"tmux", "native"}


@pytest.mark.asyncio
async def test_spawn_request_carries_resolved_backend() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

    captured: dict[str, Any] = {}

    async def fake_execute(request: SpawnRequest) -> Any:
        captured["request"] = request
        return SimpleNamespace(
            success=True,
            run_id=request.run_id,
            child_session_id=request.session_id,
            status="pending",
            pid=1,
            terminal_id="tid",
            terminal_type=request.terminal_backend,
            error=None,
            message="ok",
            locator=None,
            tmux_session_name=None,
            tmux_socket_name=None,
            tmux_socket_path=None,
            speed=None,
        )

    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.child_session_manager = MagicMock()
    runner.run_storage = MagicMock()
    runner.run_storage.has_active_run_for_task.return_value = False
    runner.terminal_manager = MagicMock()
    runner.terminal_runtime_registry = MagicMock()
    runner.write_coordinator = MagicMock()

    daemon = SimpleNamespace(terminals=TerminalConfig(default_backend="tmux"))

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "proj", "project_path": "/repo"},
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler",
            return_value=MagicMock(
                prepare_environment=AsyncMock(
                    return_value=SimpleNamespace(
                        cwd="/repo",
                        worktree_id=None,
                        clone_id=None,
                        branch_name=None,
                        extra={},
                    )
                ),
                cleanup_environment=AsyncMock(),
            ),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            side_effect=fake_execute,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn",
            return_value=prepared_spawn(),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
            return_value="21000000-0000-4000-8000-000000000001",
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.finalize_executed_spawn",
            new_callable=AsyncMock,
            return_value={"success": True, "run_id": "run"},
        ),
    ):
        native_result = await spawn_agent_impl(
            prompt="go",
            runner=runner,
            provider="claude",
            terminal_backend="native",
            daemon_config=daemon,
            project_path="/repo",
            parent_session_id="parent",
        )
        scheduled_result = await spawn_agent_impl(
            prompt="go",
            runner=runner,
            provider="claude",
            daemon_config=daemon,
            project_path="/repo",
            parent_session_id="parent",
        )
        invalid = await spawn_agent_impl(
            prompt="go",
            runner=runner,
            provider="claude",
            terminal_backend=cast(Literal["tmux", "native"] | None, "ghostty"),
            daemon_config=daemon,
            project_path="/repo",
            parent_session_id="parent",
        )

    assert native_result.get("success") is not False or captured.get("request") is not None
    request = cast(SpawnRequest, captured["request"])
    assert request.terminal_backend in {"tmux", "native"}
    del scheduled_result
    assert invalid["success"] is False
    assert "terminal_backend" in str(invalid.get("error", "")).lower() or invalid.get(
        "error_code"
    ) in {"invalid_terminal_backend", "invalid_backend"}

    explicit = SpawnRequest(
        prompt="go",
        cwd="/repo",
        provider="claude",
        session_id="s",
        run_id="r",
        parent_session_id="p",
        project_id="proj",
        prepared_spawn=prepared_spawn(),
        terminal_backend="native",
    )
    assert explicit.terminal_backend == "native"

    for path in SPAWN_REQUEST_SCAN_PATHS:
        missing = spawn_request_constructions_missing_backend(path)
        assert missing == [], (
            f"{path} SpawnRequest constructions missing terminal_backend: {missing}"
        )
