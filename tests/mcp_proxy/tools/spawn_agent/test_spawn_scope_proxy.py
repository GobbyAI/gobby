"""The limit-spawnable-agents rule on a real proxied spawn_agent call."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.hook_manager import HookManager
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("stub_srt_verifier")]

LIMIT_SPAWNABLE_AGENTS = "limit-spawnable-agents"


@pytest.fixture
def workflow_runtime() -> Iterator[WorkflowEvaluationRuntime]:
    runtime = WorkflowEvaluationRuntime()
    try:
        yield runtime
    finally:
        runtime.shutdown()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=10)


def _set_limit_rule(db: HubDatabase, *, enabled: bool) -> None:
    """Sync the bundled rules and leave only the rule under test, in the given state."""
    sync_bundled_rules(db, get_bundled_rules_path())
    manager = RuleDefinitionManager(db)
    for row in manager.list_all():
        wanted = enabled and row.name == LIMIT_SPAWNABLE_AGENTS
        if row.enabled != wanted:
            manager.update(row.id, enabled=wanted)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule_enabled", "spawned"),
    [(False, True), (True, False)],
)
async def test_listless_spawned_caller_through_the_proxy(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    mock_runner: MagicMock,
    agent_body: AgentDefinitionBody,
    workflow_runtime: WorkflowEvaluationRuntime,
    rule_enabled: bool,
    spawned: bool,
) -> None:
    project = isolated_checkout_factory(temp_db, "spawn-scope-proxy")
    repo = Path(project.root_path)
    _git(repo, "init", "-q", "-b", "main")
    _git(
        repo,
        "-c",
        "user.name=Gobby Tests",
        "-c",
        "user.email=gobby-tests@example.com",
        "commit",
        "--allow-empty",
        "--no-gpg-sign",
        "-q",
        "-m",
        "initial",
    )
    AgentDefinitionManager(temp_db).create(
        "spawn-scope-listless",
        {
            "name": "spawn-scope-listless",
            "prompts": {"agent": "Work."},
            "workflows": {"rule_selectors": {"include": ["tag:default"]}},
        },
    )
    session_manager = SessionManager(temp_db)
    root = session_manager.register(
        external_id="spawn-scope-proxy-root",
        machine_id=project.machine_id,
        source="claude",
        project_id=project.project.id,
    )
    caller = session_manager.register(
        external_id="spawn-scope-proxy-caller",
        machine_id=project.machine_id,
        source="claude",
        project_id=project.project.id,
        parent_session_id=root.id,
        agent_depth=1,
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=root.id,
        provider="claude",
        prompt="work",
        agent_name="spawn-scope-listless",
        child_session_id=caller.id,
    )
    assert session_manager.update_terminal_pickup_metadata(caller.id, agent_run_id=run.id)
    _set_limit_rule(temp_db, enabled=rule_enabled)

    # spawn_agent joins gobby-agents the way register_agent_spawn_tools merges it.
    agents_registry = InternalToolRegistry("gobby-agents", "Agent spawning")
    git_manager = WorktreeGitManager(project.root_path)
    agents_registry.merge_from(
        create_spawn_agent_registry(
            mock_runner,
            git_manager=git_manager,
            git_manager_resolver=MagicMock(return_value=git_manager),
            session_manager=session_manager,
            db=temp_db,
        )
    )
    internal_manager = InternalRegistryManager()
    internal_manager.add_registry(agents_registry)
    # The proxy reads only these HookManager attributes on the before_tool path.
    hook_manager = SimpleNamespace(
        _workflow_handler=WorkflowHookHandler(
            rule_engine=RuleEngine(temp_db, session_manager=session_manager),
            enabled=True,
            evaluation_runtime=workflow_runtime,
        ),
        _session_manager=session_manager,
        _database=temp_db,
    )
    mcp_manager = MagicMock()
    del mcp_manager.session_manager
    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=internal_manager,
        hook_manager_resolver=lambda: cast(HookManager, hook_manager),
    )
    execute = AsyncMock(
        return_value=MagicMock(success=True, run_id="run-123", child_session_id="child-456")
    )

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body", return_value=agent_body
        ),
        patch("gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn", new=execute),
    ):
        result = await proxy.call_tool(
            "gobby-agents",
            "spawn_agent",
            {
                "prompt": "Do the work.",
                "agent": "spawn-scope-worker",
                "parent_session_id": caller.id,
                "project_path": project.root_path,
                "checkout_mode": "none",
            },
            session_id=caller.id,
            enforce_workflow=True,
        )
        from gobby.mcp_proxy.tools.spawn_agent._implementation import _spawn_background_tasks

        await asyncio.gather(*tuple(_spawn_background_tasks.values()))

    if spawned:
        assert "error" not in result, result
        assert result["child_session_id"], result
        assert execute.await_count == 1
    else:
        assert result["success"] is False, result
        assert result["error_code"] == "TOOL_BLOCKED"
        assert LIMIT_SPAWNABLE_AGENTS in result["error"]
        assert execute.await_count == 0
