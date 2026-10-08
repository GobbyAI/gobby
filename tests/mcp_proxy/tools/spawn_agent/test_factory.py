"""Factory-level spawn_agent tool tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentStepWorkflowBody,
    PipelineDefinition,
    PipelineStep,
    WorkflowStep,
)
from tests.agents.prepared_spawn import prepared_spawn
from tests.fixtures.agent_definitions import make_agent_definition

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]


@pytest.mark.asyncio
@pytest.mark.parametrize("network", ["none", "trusted", None])
async def test_single_spawn_network_is_launch_local(network: str | None) -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _factory

    body = make_agent_definition(
        name="default", provider="claude", network="trusted", prompts={"agent": "Work."}
    )
    launch = AsyncMock(return_value={"success": True, "run_id": "network-launch"})
    registry = _factory.create_spawn_agent_registry(MagicMock())
    with (
        patch.object(_factory, "_load_agent_body", return_value=body),
        patch.object(_factory, "spawn_agent_impl", launch),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.get_request_principal",
            AsyncMock(return_value=None),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.get_current_session_id",
            return_value=None,
        ),
    ):
        result = await registry.call("spawn_agent", {"prompt": "work", "network": network})
        assert result["success"] is True
        assert launch.await_args is not None
        selected = launch.await_args.kwargs["agent_body"]
        assert selected.network == (network or "trusted")
        assert body.network == "trusted"
        if network is not None:
            assert selected is not body
        omitted = await registry.call("spawn_agent", {"prompt": "later"})
        assert omitted["success"] is True
        assert launch.await_args.kwargs["agent_body"] is body


@pytest.mark.asyncio
@pytest.mark.parametrize("original, final", [("none", "trusted"), ("trusted", "none")])
@pytest.mark.parametrize("network", [None, "omitted", "none", "trusted"])
async def test_network_applies_after_fallback(
    original: str, final: str, network: str | None
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _factory

    primary = make_agent_definition(
        name="primary",
        provider="claude",
        network=original,
        fallback_agent="backup",
        prompts={"agent": "Work."},
    )
    backup = make_agent_definition(
        name="backup",
        provider="codex",
        network=final,
        prompts={"agent": "Work."},
    )
    launch = AsyncMock(return_value={"success": True})
    registry = _factory.create_spawn_agent_registry(
        MagicMock(), db=MagicMock(), detection_registry=MagicMock()
    )
    with (
        patch.object(_factory, "_load_agent_body", side_effect=[primary, backup]),
        patch.object(_factory, "spawn_agent_impl", launch),
        patch(
            "gobby.agents.provider_rotation.get_failed_providers_for_task",
            return_value={"claude"},
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.get_request_principal",
            AsyncMock(return_value=None),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.get_current_session_id",
            return_value=None,
        ),
    ):
        arguments: dict[str, Any] = {"prompt": "work", "agent": "primary", "task_id": "task"}
        if network != "omitted":
            arguments["network"] = network
        result = await registry.call("spawn_agent", arguments)
    assert result["success"] is True
    assert launch.await_args is not None
    selected = launch.await_args.kwargs["agent_body"]
    assert selected.name == "backup"
    assert selected.network == (final if network == "omitted" else network or final)
    assert primary.network == original
    assert backup.network == final


@pytest.mark.asyncio
@pytest.mark.parametrize("network", ["all", "trusted", "none"])
async def test_internal_network_override_refuses_before_launch(network: str) -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _factory

    launch = AsyncMock()
    registry = _factory.create_spawn_agent_registry(MagicMock())
    with (
        patch.object(_factory, "_load_agent_body") as load,
        patch.object(_factory, "spawn_agent_impl", launch),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.get_request_principal",
            AsyncMock(side_effect=LookupError("internal")),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.get_current_session_id",
            return_value=None,
        ),
    ):
        result = await registry.call("spawn_agent", {"prompt": "work", "network": network})
    assert result["success"] is False
    assert "network" in result["error"]
    load.assert_not_called()
    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_network_override_needs_resolved_definition() -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _factory

    launch = AsyncMock()
    registry = _factory.create_spawn_agent_registry(MagicMock())
    with (
        patch.object(_factory, "_load_agent_body", return_value=None),
        patch.object(_factory, "spawn_agent_impl", launch),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.get_request_principal",
            AsyncMock(return_value=None),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.get_current_session_id",
            return_value=None,
        ),
    ):
        result = await registry.call("spawn_agent", {"prompt": "work", "network": "trusted"})
    assert result["success"] is False
    assert "resolved agent definition" in result["error"]
    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_network_unreadable_seed_refuses_before_allocations() -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _factory, _implementation

    body = make_agent_definition(name="default", provider="claude", prompts={"agent": "Work."})
    registry = _factory.create_spawn_agent_registry(MagicMock())
    project = {"id": "11111111-1111-4111-8111-111111110123", "project_path": "/test"}
    with (
        patch.object(_factory, "_load_agent_body", return_value=body),
        patch.object(_factory, "get_project_context", return_value=project),
        patch.object(_implementation, "get_project_context", return_value=project),
        patch(
            "gobby.agents.sandbox_network.trusted_domains", side_effect=OSError("seed unavailable")
        ),
        patch.object(_implementation, "preflight_placement", AsyncMock()) as placement,
        patch.object(_implementation, "get_isolation_handler") as checkout,
        patch.object(_implementation, "prepare_terminal_spawn") as child,
        patch.object(_implementation, "execute_spawn", AsyncMock()) as launch,
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.get_request_principal",
            AsyncMock(return_value=None),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.get_current_session_id",
            return_value=None,
        ),
    ):
        result = await registry.call("spawn_agent", {"prompt": "work", "network": "trusted"})
    assert result["success"] is False
    assert "Trusted seed is unreadable" in result["error"]
    placement.assert_not_awaited()
    checkout.assert_not_called()
    child.assert_not_called()
    launch.assert_not_awaited()


async def _drain_spawn_background_tasks() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._implementation import _spawn_background_tasks

    tasks = tuple(_spawn_background_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


@pytest.fixture(autouse=True)
def _stub_prelaunch_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory tests mock execute_spawn; preparation now happens first."""
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn",
        lambda *args, **kwargs: prepared_spawn(),
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.persist_initial_step_instance_if_resolved",
        lambda *args, **kwargs: None,
    )


class TestCreateSpawnAgentRegistry:
    """Tests for create_spawn_agent_registry factory function."""

    def test_creates_registry_with_correct_name(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        runner = MagicMock()
        registry = create_spawn_agent_registry(runner)

        assert registry.name == "gobby-spawn-agent"

    def test_registers_spawn_agent_tool(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        runner = MagicMock()
        registry = create_spawn_agent_registry(runner)

        assert registry.get_schema("spawn_agent") is not None

    def test_spawn_agent_schema_includes_lifecycle_defaults(self, mock_runner: MagicMock) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        schema = registry.get_schema("spawn_agent")

        assert schema is not None
        properties = schema["inputSchema"]["properties"]
        assert properties["notify_parent_on_completion"] == {
            "type": "boolean",
            "default": True,
        }
        assert properties["cleanup_isolation_on_failure"] == {
            "type": "boolean",
            "default": False,
        }
        assert "notify_parent_on_completion" not in schema["inputSchema"]["required"]
        assert "cleanup_isolation_on_failure" not in schema["inputSchema"]["required"]


class TestSpawnAgentDefaults:
    """Tests for spawn_agent with default values."""

    @pytest.mark.asyncio
    async def test_spawn_agent_defaults_to_default_agent(self, mock_runner: MagicMock) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="default",
            provider="claude",
        )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ) as mock_load,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                },
            )
            await _drain_spawn_background_tasks()

            # Verify "default" agent was loaded
            assert mock_load.call_args[0][0] == "default"
            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_spawn_agent_awaits_workflow_loader(self, mock_runner: MagicMock) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        pipeline = PipelineDefinition(
            name="review-pipeline",
            steps=[PipelineStep(id="review", exec="true")],
        )
        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._context_from_project_path",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110123",
                    "project_path": "/path/to/project",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=make_agent_definition(
                    prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                    name="default",
                    provider="claude",
                ),
            ),
            patch("gobby.workflows.pipeline_loader.PipelineLoader") as loader_class,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as mock_spawn_impl,
        ):
            loader_class.return_value.load_pipeline = AsyncMock(return_value=pipeline)

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Review the implementation",
                    "workflow": "review-pipeline",
                    "project_path": "/path/to/project",
                },
            )

        assert result["success"] is True
        loader_class.return_value.load_pipeline.assert_awaited_once_with(
            "review-pipeline",
            project_path="/path/to/project",
        )
        assert (
            mock_spawn_impl.call_args.kwargs["initial_variables"]["_assigned_pipeline"]
            == "review-pipeline"
        )

    @pytest.mark.asyncio
    async def test_spawn_agent_forwards_disabled_parent_completion_notification(
        self, mock_runner: MagicMock
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="default",
            provider="claude",
        )
        completion_registry = MagicMock()
        db = MagicMock()
        registry = create_spawn_agent_registry(
            mock_runner,
            db=db,
            completion_registry=completion_registry,
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110123",
                    "project_path": "/path/to/project",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-123"},
            ) as mock_spawn_impl,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "notify_parent_on_completion": False,
                },
            )

        assert result["success"] is True
        assert mock_spawn_impl.call_args.kwargs["parent_session_id"] == "parent-789"
        assert mock_spawn_impl.call_args.kwargs["completion_registry"] is completion_registry
        assert mock_spawn_impl.call_args.kwargs["notify_parent_on_completion"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["single", "batch-default", "batch-item"])
    async def test_spawn_agent_forwards_default_parent_completion_notification(
        self, mock_runner: MagicMock, mode: str
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="default",
            provider="claude",
        )
        completion_registry = MagicMock()
        db = MagicMock()
        registry = create_spawn_agent_registry(
            mock_runner,
            db=db,
            completion_registry=completion_registry,
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110123",
                    "project_path": "/path/to/project",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-123"},
            ) as mock_spawn_impl,
        ):
            arguments = {
                "prompt": "Test prompt",
                "parent_session_id": "parent-789",
                "extra_write_paths": ["/external/workspace"],
                "write_paths_reason": "Task authorization",
            }
            if mode == "single":
                result = await registry.call("spawn_agent", arguments)
            else:
                suggestion: dict[str, Any] = {"ref": "#1", "prompt": "Test prompt"}
                batch_arguments = {"parent_session_id": "parent-789", "suggestions": [suggestion]}
                target = batch_arguments if mode == "batch-default" else suggestion
                target.update(
                    {
                        "extra_write_paths": ["/external/workspace"],
                        "write_paths_reason": "Task authorization",
                    }
                )
                response = await registry.call("dispatch_batch", batch_arguments)
                result = response["results"][0]

        assert result["success"] is True
        assert result["run_id"] == "run-123"
        assert mock_spawn_impl.call_args.kwargs["prompt"] == "Test prompt"
        assert mock_spawn_impl.call_args.kwargs["parent_session_id"] == "parent-789"
        assert mock_spawn_impl.call_args.kwargs["completion_registry"] is completion_registry
        assert mock_spawn_impl.call_args.kwargs["notify_parent_on_completion"] is True

        assert mock_spawn_impl.call_args.kwargs["extra_write_paths"] == ["/external/workspace"]
        assert mock_spawn_impl.call_args.kwargs["write_paths_reason"] == "Task authorization"

    @pytest.mark.asyncio
    async def test_spawn_agent_derives_project_path_from_parent_session(
        self,
        mock_runner: MagicMock,
        db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from tests.fixtures.isolated_checkout import install_isolated_checkout_project

        isolated = install_isolated_checkout_project(
            db, tmp_path, name="spawn-parent-project", monkeypatch=monkeypatch
        )
        project = isolated.project
        session_manager = MagicMock()
        session_manager.resolve_session_reference.return_value = "parent-uuid"
        session_manager.get.return_value = SimpleNamespace(
            project_id=project.id, machine_id=isolated.machine_id
        )
        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="spawn-reviewer-agent",
            provider="claude",
        )
        registry = create_spawn_agent_registry(
            mock_runner,
            session_manager=session_manager,
            db=db,
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ) as mock_load,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as mock_spawn_impl,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Review the implementation",
                    "agent": "spawn-reviewer-agent",
                    "parent_session_id": "parent-ref",
                },
            )

        assert result["success"] is True
        assert mock_load.call_args.kwargs["project_id"] == project.id
        assert mock_spawn_impl.call_args.kwargs["parent_session_id"] == "parent-uuid"
        assert mock_spawn_impl.call_args.kwargs["project_path"] == isolated.root_path

    def test_parent_session_project_context_preserves_isolation_parent_fields(
        self,
        db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _parent_session_project_context
        from tests.fixtures.isolated_checkout import install_isolated_checkout_project

        project_dir = tmp_path / "worktree"
        isolated = install_isolated_checkout_project(
            db, project_dir, name="spawn-parent-project", monkeypatch=monkeypatch
        )
        # Tracked project.json parent keys are stale by contract (#21193); the
        # gitignored isolation.json sidecar is the only parent-identity source.
        (project_dir / ".gobby" / "project.json").write_text(
            json.dumps(
                {
                    "id": isolated.project.id,
                    "name": isolated.project.name,
                    "parent_project_id": "stale-tracked-parent",
                    "parent_project_path": "/stale/tracked/path",
                }
            ),
            encoding="utf-8",
        )
        (project_dir / ".gobby" / "isolation.json").write_text(
            json.dumps(
                {
                    "parent_project_id": "parent-project",
                    "parent_project_path": "/repo/main",
                }
            ),
            encoding="utf-8",
        )
        session_manager = MagicMock()
        session_manager.get.return_value = SimpleNamespace(
            project_id=isolated.project.id, machine_id=isolated.machine_id
        )

        context = _parent_session_project_context(
            parent_session_id="parent-session",
            session_manager=session_manager,
            db=db,
        )

        assert context == {
            "id": isolated.project.id,
            "name": isolated.project.name,
            "project_path": isolated.root_path,
            "parent_project_id": "parent-project",
            "parent_project_path": "/repo/main",
        }

    def test_parent_session_project_context_uses_machine_checkout(  # tdd-red window
        self,
        db: HubDatabase,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _parent_session_project_context
        from tests.fixtures.isolated_checkout import install_isolated_checkout_project

        isolated = install_isolated_checkout_project(db, tmp_path / "repo", monkeypatch=monkeypatch)
        session_manager = MagicMock()
        session_manager.get.return_value = SimpleNamespace(
            project_id=isolated.project.id,
            machine_id=isolated.machine_id,
        )

        context = _parent_session_project_context(
            parent_session_id="parent-session",
            session_manager=session_manager,
            db=db,
        )

        assert context is not None
        assert context["id"] == isolated.project.id
        assert context["project_path"] == isolated.root_path

    def test_parent_session_project_context_fails_closed_without_checkout(  # tdd-red window
        self,
        db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import (
            _UNRESOLVED_PARENT_PROJECT,
            _parent_session_project_context,
        )
        from tests.fixtures.isolated_checkout import (
            insert_isolated_machine,
            patch_local_machine_id,
        )

        machine_id = insert_isolated_machine(db)
        patch_local_machine_id(monkeypatch, machine_id)
        project = LocalProjectManager(db).create(name="spawn-missing")
        session_manager = MagicMock()
        session_manager.get.return_value = SimpleNamespace(
            project_id=project.id,
            machine_id=machine_id,
        )

        context = _parent_session_project_context(
            parent_session_id="parent-session",
            session_manager=session_manager,
            db=db,
        )

        assert context is not None
        assert context.get(_UNRESOLVED_PARENT_PROJECT) is True

    def test_parent_session_project_context_uses_unresolved_project_sentinel(
        self,
        db: HubDatabase,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import (
            _UNRESOLVED_PARENT_PROJECT,
            _parent_session_project_context,
        )

        session_manager = MagicMock()
        session_manager.get.return_value = SimpleNamespace(project_id="missing-project")

        context = _parent_session_project_context(
            parent_session_id="parent-session",
            session_manager=session_manager,
            db=db,
        )

        assert context == {
            "project_id": "missing-project",
            _UNRESOLVED_PARENT_PROJECT: True,
        }

    @pytest.mark.asyncio
    async def test_explicit_project_path_does_not_fall_back_to_current_context(
        self,
        mock_runner: MagicMock,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="default",
            provider="claude",
        )
        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._context_from_project_path",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "current-project", "project_path": "/current/project"},
            ) as mock_current_context,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ) as mock_load,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as mock_spawn_impl,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Use explicit path",
                    "project_path": "/explicit/project",
                },
            )

        assert result["success"] is True
        mock_current_context.assert_not_called()
        assert mock_load.call_args.kwargs["project_id"] is None
        assert mock_spawn_impl.call_args.kwargs["project_path"] == "/explicit/project"


class TestSpawnAgentParamOverrides:
    """Tests for tool params overriding agent definition values."""

    @pytest.mark.asyncio
    async def test_state_dispatch_developer_is_one_shot(
        self,
        mock_runner: MagicMock,
        temp_db: HubDatabase,
        sample_project: dict[str, Any],
    ) -> None:
        from gobby.dispatch.actions import SpawnAgentAction
        from gobby.dispatch.spawn import spawn_agent
        from gobby.storage.sessions import SessionManager
        from gobby.storage.tasks import LocalTaskManager
        from tests.dispatch.test_dispatcher import _task

        body = make_agent_definition(
            name="developer",
            provider="claude",
            execution_mode="interactive",
            prompts={"agent": "Implement the task."},
        )
        task = _task(temp_db, sample_project, checkout_mode="none")
        services = SimpleNamespace(
            database=temp_db,
            task_manager=LocalTaskManager(temp_db),
            session_manager=SessionManager(temp_db),
            agent_runner=mock_runner,
        )

        async def invoke() -> dict[str, Any]:
            with patch("gobby.workflows.agent_resolver.resolve_agent", return_value=body):
                run_id = await spawn_agent(
                    SpawnAgentAction(
                        task_id=task.id,
                        task_ref=f"#{task.seq_num}",
                        agent_slug="developer",
                        prompt="Implement the task.",
                    ),
                    db=temp_db,
                    services=services,
                )
            return {"success": True, "run_id": run_id}

        request = await self._spawn_request_for(mock_runner, body, {}, invoke=invoke)
        assert request.resume_metadata_json["execution_mode"] == "one_shot"
        assert "Remain available between turns" not in request.prompt

    @pytest.mark.asyncio
    async def test_dispatch_batch_worker_is_one_shot(self, mock_runner: MagicMock) -> None:
        body = make_agent_definition(
            name="developer",
            provider="claude",
            execution_mode="interactive",
            prompts={"agent": "Implement the task."},
        )
        request = await self._spawn_request_for(
            mock_runner,
            body,
            {"suggestions": [{"ref": "#1", "prompt": "Implement the task."}]},
            tool_name="dispatch_batch",
        )
        assert request.resume_metadata_json["execution_mode"] == "one_shot"
        assert "Remain available between turns" not in request.prompt

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("definition_mode", "ttl", "override", "expected_ttl"),
        [
            ("interactive", 900, None, 900),
            ("interactive", None, None, None),
            ("interactive", 900, "one_shot", None),
            ("one_shot", None, "interactive", None),
        ],
    )
    async def test_idle_ttl_is_persisted_only_for_interactive_runs(
        self,
        mock_runner: MagicMock,
        definition_mode: str,
        ttl: int | None,
        override: str | None,
        expected_ttl: int | None,
    ) -> None:
        body = make_agent_definition(
            name="default",
            provider="claude",
            prompts={"agent": "Run the assigned task."},
            execution_mode=definition_mode,
            idle_ttl_seconds=ttl,
        )
        params: dict[str, object] = {}
        if override is not None:
            params["execution_mode"] = override
        request = await self._spawn_request_for(mock_runner, body, params)
        if expected_ttl is None:
            assert "idle_ttl_seconds" not in request.resume_metadata_json
        else:
            assert request.resume_metadata_json["idle_ttl_seconds"] == expected_ttl
        assert "idle_ttl_seconds" not in request.initial_variables

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("definition_mode", "override", "expected"),
        [
            ("one_shot", None, "one_shot"),
            ("interactive", None, "interactive"),
            ("one_shot", "interactive", "interactive"),
            ("interactive", "one_shot", "one_shot"),
        ],
    )
    async def test_execution_mode_is_persisted_for_watchdog_and_resume(
        self,
        mock_runner: MagicMock,
        definition_mode: str,
        override: str | None,
        expected: str,
    ) -> None:
        body = make_agent_definition(
            name="default",
            provider="claude",
            prompts={"agent": "Run the assigned task."},
            execution_mode=definition_mode,
        )
        params: dict[str, object] = {}
        if override is not None:
            params["execution_mode"] = override
        request = await self._spawn_request_for(mock_runner, body, params)
        assert request.resume_metadata_json["execution_mode"] == expected
        assert request.initial_variables["execution_mode"] == expected
        if expected == "interactive":
            assert "Remain available between turns" in request.prompt

    async def _spawn_request_for(
        self,
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
        call_params: dict[str, object],
        *,
        tool_name: str = "spawn_agent",
        invoke: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> Any:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110123",
                    "project_path": "/path/to/project",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110123",
                    "project_path": "/path/to/project",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            params: dict[str, object] = {"parent_session_id": "parent-789"}
            if tool_name == "spawn_agent":
                params["prompt"] = "Test prompt"
            params.update(call_params)
            result = (
                await invoke() if invoke is not None else await registry.call(tool_name, params)
            )
            if tool_name == "dispatch_batch":
                result = result["results"][0]
            await _drain_spawn_background_tasks()

            assert result["success"] is True, result
            return mock_execute.call_args[0][0]

    @pytest.mark.asyncio
    async def test_tool_params_override_agent_definition(self, mock_runner: MagicMock) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="default",
            provider="claude",
        )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                },
            )
            await _drain_spawn_background_tasks()

            assert result["success"] is True
            assert mock_execute.call_args[0][0].provider == "claude"

    @pytest.mark.asyncio
    async def test_provider_override_substitutes_same_tier_model(
        self, mock_runner: MagicMock
    ) -> None:
        """The agent's model cannot cross providers, but the target CLI must not choose one."""
        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="merge-worker",
            provider="codex",
            model="gpt-5.6-sol",
        )

        spawn_request = await self._spawn_request_for(
            mock_runner,
            agent_body,
            {
                "agent": "merge-worker",
                "provider": "claude",
            },
        )

        assert spawn_request.provider == "claude"
        assert spawn_request.model == "opus"

    @pytest.mark.asyncio
    async def test_provider_override_preserves_explicit_model(self, mock_runner: MagicMock) -> None:
        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="merge-worker",
            provider="codex",
            model="gpt-5.4",
        )

        spawn_request = await self._spawn_request_for(
            mock_runner,
            agent_body,
            {
                "agent": "merge-worker",
                "provider": "claude",
                "model": "opus",
            },
        )

        assert spawn_request.provider == "claude"
        assert spawn_request.model == "opus"

    @pytest.mark.asyncio
    async def test_provider_override_blank_model_still_substitutes_same_tier(
        self, mock_runner: MagicMock
    ) -> None:
        """A blank model is not a choice, so the tier substitution still applies."""
        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="merge-worker",
            provider="codex",
            model="gpt-5.6-sol",
        )

        spawn_request = await self._spawn_request_for(
            mock_runner,
            agent_body,
            {
                "agent": "merge-worker",
                "provider": "claude",
                "model": "   ",
            },
        )

        assert spawn_request.provider == "claude"
        assert spawn_request.model == "opus"

    @pytest.mark.asyncio
    async def test_no_provider_override_keeps_agent_definition_model(
        self, mock_runner: MagicMock
    ) -> None:
        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="merge-worker",
            provider="codex",
            model="gpt-5.4",
        )

        spawn_request = await self._spawn_request_for(
            mock_runner,
            agent_body,
            {
                "agent": "merge-worker",
            },
        )

        assert spawn_request.provider == "codex"
        assert spawn_request.model == "gpt-5.4"

    @pytest.mark.asyncio
    async def test_model_selector_does_not_override_agent_provider(
        self, mock_runner: MagicMock
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import (
            PROVIDER_REQUIRED_FOR_MODEL,
        )

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="merge-worker",
            provider="codex",
            model="gpt-5.4",
        )
        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "agent": "merge-worker",
                    "model": "claude/sonnet-4-6",
                },
            )

        assert result["success"] is False
        assert result["error_code"] == PROVIDER_REQUIRED_FOR_MODEL
        mock_execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_model_name_does_not_infer_provider(self, mock_runner: MagicMock) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import (
            PROVIDER_REQUIRED_FOR_MODEL,
        )

        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="merge-worker",
            provider="claude",
            model="sonnet-4-6",
        )
        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "agent": "merge-worker",
                    "model": "gpt-5.6-sol",
                },
            )

        assert result["success"] is False
        assert result["error_code"] == PROVIDER_REQUIRED_FOR_MODEL
        mock_execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_provider_accepts_opaque_model_selector(
        self, mock_runner: MagicMock
    ) -> None:
        agent_body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="merge-worker",
            provider="codex",
            model="gpt-5.4",
        )

        spawn_request = await self._spawn_request_for(
            mock_runner,
            agent_body,
            {
                "agent": "merge-worker",
                "provider": "codex",
                "model": "claude/sonnet-4-6",
            },
        )

        assert spawn_request.provider == "codex"
        assert spawn_request.model == "claude/sonnet-4-6"

    @pytest.mark.asyncio
    async def test_missing_provider_sources_returns_actionable_error(
        self, mock_runner: MagicMock
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={
                    "id": "11111111-1111-4111-8111-111111110123",
                    "project_path": "/path/to/project",
                },
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test prompt", "model": "gpt-5.6-sol"},
            )

        assert result["success"] is False
        assert result["error_code"] == "provider_required_for_model"
        assert result["model"] == "gpt-5.6-sol"
        assert "explicit provider" in result["error"]
        mock_execute.assert_not_called()


class TestSpawnAgentTaskResolution:
    """Tests for task_id resolution formats."""

    @pytest.mark.asyncio
    async def test_task_id_supports_hash_n_format(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody, db: HubDatabase
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.title = "Implement feature"
        mock_task.seq_num = 6100
        mock_task.id = "uuid-123"
        mock_task_manager.get_task.return_value = mock_task

        registry = create_spawn_agent_registry(
            mock_runner,
            task_manager=mock_task_manager,
            db=db,
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.resolve_task_id_for_mcp"
            ) as mock_resolve,
            patch("gobby.mcp_proxy.tools.spawn_agent._implementation.TaskSpawnLease") as lease_cls,
        ):
            lease = lease_cls.return_value
            lease.acquire.return_value = None
            lease.attach.return_value = None
            lease.release_unattached.return_value = None
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_resolve.return_value = "uuid-123"

            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "task_id": "#6100",
                },
            )
            await _drain_spawn_background_tasks()

            mock_resolve.assert_called_once()
            assert mock_resolve.call_count == 1
            assert mock_resolve.call_args is not None
            assert result["success"] is True


class TestSpawnAgentSandbox:
    """Tests for daemon-owned agent sandbox defaults."""

    @pytest.mark.asyncio
    async def test_agent_sandbox_defaults_come_from_daemon_config(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(
            mock_runner,
            db=MagicMock(),
            config_resolver=lambda: MagicMock(
                agent_sandbox=MagicMock(
                    enabled=False,
                    mode="restrictive",
                    allow_network=False,
                    extra_read_paths=["/tmp/agent-read"],
                    extra_write_paths=["/tmp/agent-write"],
                ),
            ),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                },
            )
            await _drain_spawn_background_tasks()

            # The resolver's disabled sandbox reaches the managed-SRT gate and is refused.
            assert result["success"] is False
            assert result["error_code"] == "sandbox_required"
            mock_get_handler.assert_not_called()
            mock_execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawn_agent_schema_no_longer_exposes_sandbox_knobs(
        self, mock_runner: MagicMock, agent_body: AgentDefinitionBody
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        schema = registry.get_schema("spawn_agent")

        assert schema is not None
        properties = schema["inputSchema"]["properties"]
        assert "sandbox" not in properties
        assert "sandbox_mode" not in properties
        assert "sandbox_allow_network" not in properties
        assert "sandbox_extra_paths" not in properties


class TestSpawnAgentNotFound:
    """Tests for agent not found behavior."""

    @pytest.mark.asyncio
    async def test_returns_error_for_missing_non_default_agent(
        self, mock_runner: MagicMock
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
            return_value=None,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test",
                    "parent_session_id": "parent-789",
                    "agent": "nonexistent",
                },
            )

            assert result["success"] is False
            assert "not found" in result["error"].lower()


class TestSpawnAgentPromptPreamble:
    """Tests for prompt handling — preamble is injected via hooks, not prompt."""

    @pytest.mark.asyncio
    async def test_prompt_passed_without_preamble(self, mock_runner: MagicMock) -> None:
        """Preamble is injected via session_start hooks, not prepended to prompt."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = make_agent_definition(
            prompts={"agent": "## Role\nBackend developer\n\nWrite clean code."},
            name="dev",
            provider="claude",
        )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            await registry.call(
                "spawn_agent",
                {
                    "prompt": "Fix the bug",
                    "parent_session_id": "parent-789",
                },
            )
            await _drain_spawn_background_tasks()

            # Prompt is passed through as-is; preamble injected via hooks
            spawn_request = mock_execute.call_args[0][0]
            assert spawn_request.prompt == "Fix the bug"
            assert spawn_request.agent_name == "default"


class TestPreparedSnapshotCreation:
    """Registration is gone; spawn persists a typed snapshot instead."""

    def test_register_symbol_is_deleted(self) -> None:
        import gobby.mcp_proxy.tools.spawn_agent._factory as factory

        assert not hasattr(factory, "_register_agent_step_workflow")

    def test_persist_initial_step_instance_creates_snapshot(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._step_state import persist_initial_step_instance

        db = MagicMock()
        saved: list[Any] = []

        class _Manager:
            def save(self, instance: Any) -> None:
                saved.append(instance)

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._step_state.AgentStepInstanceManager",
            return_value=_Manager(),
        ):
            body = make_agent_definition(
                prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                name="rogue-agent",
                step_workflow=AgentStepWorkflowBody(steps=[WorkflowStep(name="claim")]),
            )
            persist_initial_step_instance(
                db,
                body,
                session_id="11111111-1111-4111-8111-111111111111",
                step_workflow_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
        assert len(saved) == 1
        assert saved[0].agent_name == "rogue-agent"
        assert saved[0].current_step == "claim"
        assert saved[0].snapshot.steps[0].name == "claim"

    def test_persist_if_resolved_skips_missing_agent_definition(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._step_state import (
            persist_initial_step_instance_if_resolved,
        )

        db = MagicMock()
        body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="missing-agent",
            step_workflow=AgentStepWorkflowBody(steps=[WorkflowStep(name="claim")]),
        )
        with (
            patch(
                "gobby.workflows.agent_resolver.resolve_agent_with_row",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._step_state.persist_initial_step_instance"
            ) as persist,
        ):
            persisted = persist_initial_step_instance_if_resolved(
                db,
                body,
                session_id="11111111-1111-4111-8111-111111111111",
                project_id="proj-1",
            )

        assert persisted is False
        persist.assert_not_called()

    def test_persist_if_resolved_writes_when_definition_exists(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._step_state import (
            persist_initial_step_instance_if_resolved,
        )

        db = MagicMock()
        body = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="coder",
            step_workflow=AgentStepWorkflowBody(steps=[WorkflowStep(name="claim")]),
        )
        row = SimpleNamespace(step_workflow_id="wf-1")
        with (
            patch(
                "gobby.workflows.agent_resolver.resolve_agent_with_row",
                return_value=(body, row),
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._step_state.persist_initial_step_instance"
            ) as persist,
        ):
            persisted = persist_initial_step_instance_if_resolved(
                db,
                body,
                session_id="11111111-1111-4111-8111-111111111111",
                project_id="proj-1",
            )

        assert persisted is True
        persist.assert_called_once_with(
            db,
            body,
            session_id="11111111-1111-4111-8111-111111111111",
            step_workflow_id="wf-1",
            initial_variables=None,
        )
