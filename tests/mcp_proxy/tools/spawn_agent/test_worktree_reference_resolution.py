"""spawn_agent and dispatch_batch resolve ``worktree_id`` refs before storage (#22377).

Handoffs surface eight-character worktree IDs and every gobby-worktrees tool
accepts them as unique id prefixes, but the spawn path passed the raw ref
straight into ``LocalWorktreeManager.get``/``.delete`` and surfaced psycopg's
``invalid input syntax for type uuid``. Both spawn entry points now resolve
through ``LocalWorktreeManager.resolve_reference`` first: full UUIDs and unique
prefixes reuse the matching worktree, while ambiguous or unknown refs fail
cleanly before any terminal or worktree exists.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import Project
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents.prepared_spawn import prepared_spawn
from tests.fixtures.isolated_checkout import (
    IsolatedCheckoutProject,
    install_isolated_checkout_project,
)

pytestmark = pytest.mark.unit

_SEEDED_TEST_MACHINE_ID = "21000000-0000-4000-8000-000000000002"
_FULL_ID = "0b0b0b0b-3333-4333-8333-333333333333"
_OTHER_ID = "0b0b0c0c-4444-4444-8444-444444444444"
_UNIQUE_PREFIX = "0b0b0b0b"
_AMBIGUOUS_PREFIX = "0b0b"
_UNKNOWN_PREFIX = "deadbeef"

_RESOLVABLE_REFS = [
    pytest.param(_FULL_ID, id="full-uuid"),
    pytest.param(_UNIQUE_PREFIX, id="unique-prefix"),
]
_UNRESOLVABLE_REFS = [
    pytest.param(_AMBIGUOUS_PREFIX, "Ambiguous worktree reference", id="ambiguous-prefix"),
    pytest.param(_UNKNOWN_PREFIX, "not found", id="unknown-prefix"),
]


@pytest.fixture
def checkout(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> IsolatedCheckoutProject:
    """An isolated checkout whose seeded machine is pinned as this machine."""
    return install_isolated_checkout_project(
        temp_db,
        tmp_path / "isolated-checkout",
        machine_id=_SEEDED_TEST_MACHINE_ID,
        monkeypatch=monkeypatch,
    )


@pytest.fixture
def storage(
    temp_db: HubDatabase, tmp_path: Path, checkout: IsolatedCheckoutProject
) -> LocalWorktreeManager:
    """Two on-disk worktrees; ``_FULL_ID`` is the one spawns are meant to reuse."""
    manager = LocalWorktreeManager(temp_db)
    for worktree_id, directory, branch in (
        (_FULL_ID, "target-worktree", "feature/target"),
        (_OTHER_ID, "other-worktree", "feature/other"),
    ):
        worktree_path = tmp_path / directory
        worktree_path.mkdir()
        created = manager.create(
            project_id=checkout.project.id,
            branch_name=branch,
            worktree_path=str(worktree_path),
            agent_session_id=None,
        )
        temp_db.execute(
            "UPDATE worktrees SET id = %s WHERE id = %s",
            (worktree_id, created.id),
        )
    return manager


def _git_manager() -> MagicMock:
    git_manager = MagicMock()
    git_manager.get_current_branch = AsyncMock(return_value="main")
    return git_manager


def _spawn_registry(
    runner: MagicMock, worktree_storage: LocalWorktreeManager, git_manager: MagicMock
) -> InternalToolRegistry:
    return create_spawn_agent_registry(
        runner,
        worktree_storage=worktree_storage,
        git_manager=git_manager,
        db=MagicMock(),
    )


@contextmanager
def _spawn_flow(
    agent_body: AgentDefinitionBody, checkout: IsolatedCheckoutProject
) -> Iterator[AsyncMock]:
    """Patch the spawn internals past agent load, worktree sync, and execution."""
    execute_spawn = AsyncMock(
        return_value=MagicMock(
            success=True,
            child_session_id="c-1",
            status="ok",
            pid=1,
            backend=None,
            terminal_id=None,
            message="ok",
            process=None,
        )
    )
    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
            return_value=agent_body,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": checkout.project.id, "project_path": checkout.root_path},
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.sync_reused_worktree_to_base",
            new=AsyncMock(return_value=SimpleNamespace(base_commit_sha="base-sha")),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment",
            new=AsyncMock(),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
            return_value=None,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            new=execute_spawn,
        ),
    ):
        yield execute_spawn


async def _drain_spawn_background_tasks() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._implementation import _spawn_background_tasks

    tasks = tuple(_spawn_background_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


class TestSpawnAgentResolvesWorktreeReferences:
    """The spawn_agent entry point accepts full UUIDs and unique prefixes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("worktree_ref", _RESOLVABLE_REFS)
    async def test_reuses_resolved_worktree(
        self,
        storage: LocalWorktreeManager,
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
        checkout: IsolatedCheckoutProject,
        worktree_ref: str,
    ) -> None:
        registry = _spawn_registry(mock_runner, storage, _git_manager())
        with _spawn_flow(agent_body, checkout) as execute_spawn:
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Continue the assigned task",
                    "provider": "claude",
                    "terminal_backend": "tmux",
                    "parent_session_id": "parent-1",
                    "worktree_id": worktree_ref,
                },
            )
            await _drain_spawn_background_tasks()
        assert result["success"] is True, result
        assert result["worktree_id"] == _FULL_ID
        assert execute_spawn.await_args is not None
        assert execute_spawn.await_args.args[0].worktree_id == _FULL_ID

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("worktree_ref", "error_needle"), _UNRESOLVABLE_REFS)
    async def test_returns_resolver_error_before_any_allocation(
        self,
        storage: LocalWorktreeManager,
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
        checkout: IsolatedCheckoutProject,
        worktree_ref: str,
        error_needle: str,
    ) -> None:
        registry = _spawn_registry(mock_runner, storage, _git_manager())
        with (
            _spawn_flow(agent_body, checkout) as execute_spawn,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn",
                new=MagicMock(return_value=prepared_spawn()),
            ) as prepare_terminal,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Continue the assigned task",
                    "provider": "claude",
                    "terminal_backend": "tmux",
                    "parent_session_id": "parent-1",
                    "worktree_id": worktree_ref,
                },
            )
            await _drain_spawn_background_tasks()
        assert result["success"] is False, result
        assert error_needle in result["error"]
        assert "invalid input syntax" not in result["error"]
        execute_spawn.assert_not_awaited()
        prepare_terminal.assert_not_called()
        assert storage.get(_FULL_ID) is not None
        assert storage.get(_OTHER_ID) is not None


class TestDispatchBatchResolvesWorktreeReferences:
    """The dispatch_batch entry point accepts full UUIDs and unique prefixes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("worktree_ref", _RESOLVABLE_REFS)
    async def test_batch_agents_reuse_resolved_worktree(
        self,
        storage: LocalWorktreeManager,
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
        checkout: IsolatedCheckoutProject,
        worktree_ref: str,
    ) -> None:
        registry = _spawn_registry(mock_runner, storage, _git_manager())
        with _spawn_flow(agent_body, checkout) as execute_spawn:
            result = await registry.call(
                "dispatch_batch",
                {
                    "suggestions": [{"task_ref": "22377", "prompt": "Continue the assigned task"}],
                    "agent": "default",
                    "provider": "claude",
                    "parent_session_id": "parent-1",
                    "worktree_id": worktree_ref,
                },
            )
            await _drain_spawn_background_tasks()
        assert result["dispatched"] == 1, result
        entry = result["results"][0]
        assert entry["success"] is True, entry
        assert execute_spawn.await_args is not None
        assert execute_spawn.await_args.args[0].worktree_id == _FULL_ID

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("worktree_ref", "error_needle"), _UNRESOLVABLE_REFS)
    async def test_batch_refusal_reports_resolver_error_without_allocation(
        self,
        storage: LocalWorktreeManager,
        mock_runner: MagicMock,
        agent_body: AgentDefinitionBody,
        checkout: IsolatedCheckoutProject,
        worktree_ref: str,
        error_needle: str,
    ) -> None:
        registry = _spawn_registry(mock_runner, storage, _git_manager())
        with (
            _spawn_flow(agent_body, checkout) as execute_spawn,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn",
                new=MagicMock(return_value=prepared_spawn()),
            ) as prepare_terminal,
        ):
            result = await registry.call(
                "dispatch_batch",
                {
                    "suggestions": [{"task_ref": "22377", "prompt": "Continue the assigned task"}],
                    "agent": "default",
                    "provider": "claude",
                    "parent_session_id": "parent-1",
                    "worktree_id": worktree_ref,
                },
            )
            await _drain_spawn_background_tasks()
        assert result["dispatched"] == 0, result
        entry = result["results"][0]
        assert entry["success"] is False, entry
        assert error_needle in entry["error"]
        assert "invalid input syntax" not in entry["error"]
        execute_spawn.assert_not_awaited()
        prepare_terminal.assert_not_called()
        assert storage.get(_FULL_ID) is not None
        assert storage.get(_OTHER_ID) is not None
