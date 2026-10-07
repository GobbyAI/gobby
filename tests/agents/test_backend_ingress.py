"""Plan 1.6: no spawn ingress selects a terminal backend; lifetime is internal."""

from __future__ import annotations

import ast
import dataclasses
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.spawn_models import SpawnRequest
from gobby.cli.agents import spawn_agent_cmd
from gobby.config.terminals import TerminalConfig
from gobby.dispatch._planning_enhancement import _spawn_plan_enhancer
from gobby.dispatch._rule_actions import _spawn_stage_agent
from gobby.dispatch.actions import SpawnAgentAction
from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.servers.routes.agent_spawn import AgentSpawnRequest, BatchSpawnRequest
from gobby.storage.definitions._shared import encode_json_value
from gobby.storage.definitions.agents import AgentDefinitionManager, parent_body
from gobby.storage.hub.protocol import HubDatabase
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.agents.prepared_spawn import prepared_spawn
from tests.fixtures.agent_definitions import make_agent_definition

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]

ROOT = Path(__file__).resolve().parents[2]
REJECTION = "terminal_backend is not accepted"
DETECTION_REGISTRY = cast(DetectionManifestRegistry, BundledDetectionRegistry())
TASK_UUID = "7d34e462-6ba3-5a6c-b1c6-1584b855cb83"


def _impl_call_keywords(path: Path) -> set[str]:
    """Keyword names passed to ``spawn_agent_impl(...)`` anywhere in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keywords: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "spawn_agent_impl"
        ):
            keywords.update(kw.arg for kw in node.keywords if kw.arg is not None)
    return keywords


def _spawn_request_keywords(path: Path) -> set[str]:
    """Keyword names passed to every ``SpawnRequest(...)`` construction in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keywords: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "SpawnRequest")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "SpawnRequest")
        ):
            keywords.update(kw.arg for kw in node.keywords if kw.arg is not None)
    return keywords


def _legacy_body(name: str) -> dict[str, Any]:
    body = make_agent_definition(prompts={"agent": "Run the task."}, name=name)
    return {**body.model_dump(mode="json"), "terminal_backend": "native"}


def test_every_agent_definition_write_and_read_rejects_backend(temp_db: HubDatabase) -> None:
    manager = AgentDefinitionManager(temp_db)
    legacy = _legacy_body("legacy-backend")

    with pytest.raises(ValueError, match=REJECTION):
        parent_body(legacy)
    with pytest.raises(ValueError, match=REJECTION):
        parent_body(encode_json_value(legacy) or "")
    with pytest.raises(ValueError, match=REJECTION):
        manager.create(name="legacy-backend", definition_json=legacy, enabled=True)
    with pytest.raises(ValueError, match=REJECTION):
        manager.upsert_with_steps("legacy-backend", legacy, None)
    with pytest.raises(ValueError, match=REJECTION):
        manager.upsert_from_sync("legacy-backend", legacy, None)
    assert manager.get_by_name("legacy-backend") is None

    clean = {key: value for key, value in legacy.items() if key != "terminal_backend"}
    row = manager.create(name="legacy-backend", definition_json=clean, enabled=True)
    with pytest.raises(ValueError, match=REJECTION):
        manager.update(row.id, definition_json=legacy)
    assert "terminal_backend" not in manager.get(row.id).definition_json

    # A row stored before the rejection existed still resolves to a typed refusal.
    with temp_db.transaction() as conn:
        conn.execute(
            "UPDATE agent_definitions SET definition_json = %s WHERE id = %s",
            (encode_json_value(legacy), row.id),
        )
    assert manager.get(row.id).definition_json["terminal_backend"] == "native"
    with pytest.raises(ValueError, match=REJECTION):
        _load_agent_body("legacy-backend", temp_db)


@pytest.mark.asyncio
async def test_legacy_definition_spawn_returns_typed_refusal(temp_db: HubDatabase) -> None:
    manager = AgentDefinitionManager(temp_db)
    legacy = _legacy_body("legacy-spawn")
    clean = {key: value for key, value in legacy.items() if key != "terminal_backend"}
    row = manager.create(name="legacy-spawn", definition_json=clean, enabled=True)
    with temp_db.transaction() as conn:
        conn.execute(
            "UPDATE agent_definitions SET definition_json = %s WHERE id = %s",
            (encode_json_value(legacy), row.id),
        )
    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
            new_callable=AsyncMock,
        ) as mock_impl,
    ):
        registry = create_spawn_agent_registry(
            MagicMock(), db=temp_db, detection_registry=DETECTION_REGISTRY
        )
        tool_fn = registry.get_tool("spawn_agent")
        assert tool_fn is not None
        result = await tool_fn(prompt="go", agent="legacy-spawn", task_id="task-1")
    assert result["success"] is False
    assert REJECTION in result["error"]
    mock_impl.assert_not_called()


@pytest.mark.asyncio
async def test_every_public_spawn_ingress_rejects_backend() -> None:
    # MCP: the generated schema has no selector and a literal legacy payload fails.
    registry = create_spawn_agent_registry(MagicMock(), db=MagicMock())
    schema = registry.get_schema("spawn_agent")
    assert schema is not None
    assert "terminal_backend" not in schema["inputSchema"]["properties"]
    with pytest.raises(ValueError, match="Unknown argument.*terminal_backend"):
        await registry.call("spawn_agent", {"prompt": "go", "terminal_backend": "native"})
    assert "terminal_backend" not in inspect.signature(spawn_agent_impl).parameters

    # HTTP: single and batch bodies reject the field as unknown.
    assert "terminal_backend" not in AgentSpawnRequest.model_fields
    with pytest.raises(ValidationError, match="terminal_backend"):
        AgentSpawnRequest.model_validate({"task_id": "#1", "terminal_backend": "native"})
    with pytest.raises(ValidationError, match="terminal_backend"):
        BatchSpawnRequest.model_validate(
            {"spawns": [{"task_id": "#1", "terminal_backend": "native"}]}
        )

    # CLI: no option exists, so passing one is a usage error.
    assert all(param.name != "terminal_backend" for param in spawn_agent_cmd.params)
    cli = CliRunner().invoke(
        spawn_agent_cmd, ["go", "--session", "#1", "--terminal-backend", "native"]
    )
    assert cli.exit_code == 2
    assert "--terminal-backend" in cli.output

    # Dispatch: the action has no field and both constructors build it without one.
    assert "terminal_backend" not in {f.name for f in dataclasses.fields(SpawnAgentAction)}
    with pytest.raises(TypeError, match="terminal_backend"):
        cast(Any, SpawnAgentAction)(
            task_id=TASK_UUID,
            task_ref="#1",
            agent_slug="backend-developer",
            prompt="go",
            terminal_backend="native",
        )
    stage = SimpleNamespace(name="planning", stage_name="planning", state="ready", position=0)
    task = SimpleNamespace(id=TASK_UUID, ref="#1", additional_skills=())
    context = SimpleNamespace(prompt_context={})
    rule_action = _spawn_stage_agent(task, stage, context, "planner")
    enhancement = _spawn_plan_enhancer(task, stage, context, round_number=1, max_rounds=2)
    assert not hasattr(rule_action, "terminal_backend")
    assert not hasattr(enhancement, "terminal_backend")

    # Every spawn_agent_impl forwarder passes no backend string.
    for forwarder in (
        "src/gobby/dispatch/spawn.py",
        "src/gobby/scheduler/executor.py",
        "src/gobby/servers/routes/agent_spawn.py",
        "src/gobby/mcp_proxy/tools/spawn_agent/_factory.py",
    ):
        keywords = _impl_call_keywords(ROOT / forwarder)
        assert keywords, forwarder
        assert "terminal_backend" not in keywords, forwarder


@pytest.mark.asyncio
async def test_all_spawn_request_producers_emit_run_lifetime() -> None:
    fields = {f.name: f for f in dataclasses.fields(SpawnRequest)}
    assert "terminal_backend" not in fields
    assert fields["terminal_lifetime"].default == "run"

    bare = SpawnRequest(
        prompt="go",
        cwd="/repo",
        provider="claude",
        session_id="s",
        run_id="r",
        parent_session_id="p",
        project_id="proj",
        prepared_spawn=prepared_spawn(),
    )
    assert bare.terminal_lifetime == "run"

    captured: list[SpawnRequest] = []

    async def fake_execute(request: SpawnRequest) -> Any:
        captured.append(request)
        return SimpleNamespace(
            success=True,
            run_id=request.run_id,
            child_session_id=request.session_id,
            status="pending",
            pid=1,
            terminal_id="tid",
            terminal_type="native",
            error=None,
            message="ok",
            locator=None,
            tmux_session_name=None,
            speed=None,
        )

    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.run_storage.has_active_run_for_task.return_value = False
    module = "gobby.mcp_proxy.tools.spawn_agent._implementation"
    scheduled: list[Callable[[], Awaitable[object]]] = []

    def capture_launch(
        _tasks: object, _key: str, factory: Callable[[], Awaitable[object]], **_: object
    ) -> None:
        scheduled.append(factory)

    with (
        patch(f"{module}.schedule_background_task", side_effect=capture_launch),
        patch(
            f"{module}.get_project_context",
            return_value={"id": "proj", "project_path": "/repo"},
        ),
        patch(
            f"{module}.get_isolation_handler",
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
        patch(f"{module}.execute_spawn", side_effect=fake_execute),
        patch(f"{module}.prepare_terminal_spawn", return_value=prepared_spawn()),
        patch(f"{module}.get_machine_id", return_value="21000000-0000-4000-8000-000000000001"),
        patch(
            f"{module}.finalize_executed_spawn",
            new_callable=AsyncMock,
            return_value={"success": True, "run_id": "run"},
        ),
    ):
        result = await spawn_agent_impl(
            prompt="go",
            runner=runner,
            provider="claude",
            daemon_config=SimpleNamespace(terminals=TerminalConfig()),
            project_path="/repo",
            parent_session_id="parent",
        )
        assert len(scheduled) == 1, result
        await scheduled[0]()
    assert [request.terminal_lifetime for request in captured] == ["run"]

    # No production SpawnRequest producer authors a backend or a lifetime: omission
    # is the only shape, and it materializes as run.
    producers = [
        path
        for path in (ROOT / "src" / "gobby").rglob("*.py")
        if "SpawnRequest(" in path.read_text(encoding="utf-8")
    ]
    assert producers
    for path in producers:
        keywords = _spawn_request_keywords(path)
        assert "terminal_backend" not in keywords, path
        assert "terminal_lifetime" not in keywords, path
