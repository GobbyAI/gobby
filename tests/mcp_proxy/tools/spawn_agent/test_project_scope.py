"""Cross-project scoping regressions for agent isolation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext, SpawnConfig
from gobby.agents.isolation_worktree import WorktreeIsolationHandler
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskArtifactManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory, IsolatedCheckoutProject

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _initialize_repo(project: IsolatedCheckoutProject, branch: str, identity: str) -> str:
    repo = Path(project.root_path)
    _git(repo, "init", "-q", "-b", branch)
    (repo / "identity.txt").write_text(identity)
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Gobby Tests",
        "-c",
        "user.email=gobby-tests@example.com",
        "commit",
        "--no-gpg-sign",
        "-q",
        "-m",
        "initial",
    )
    return _git(repo, "rev-parse", "HEAD")


def _register_parent(
    db: HubDatabase,
    project: IsolatedCheckoutProject,
    external_id: str,
) -> str:
    return SessionManager(db).register_session(
        external_id=external_id,
        machine_id=project.machine_id,
        source="test",
        project_id=project.project.id,
        title="Parent",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("targeting", ["explicit_path", "parent_session"])
async def test_worktree_spawn_uses_target_repository(
    targeting: str,
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    mock_runner: MagicMock,
    agent_body: AgentDefinitionBody,
) -> None:
    """Target selection controls branch detection, worktree source, and code-index cwd."""
    daemon_project = isolated_checkout_factory(temp_db, f"daemon-{targeting}")
    target_project = isolated_checkout_factory(temp_db, f"target-{targeting}")
    _initialize_repo(daemon_project, "daemon-base", "daemon")
    target_sha = _initialize_repo(target_project, "target-base", "target")

    parent_project = daemon_project if targeting == "explicit_path" else target_project
    parent_session_id = _register_parent(temp_db, parent_project, f"parent-{targeting}")
    target_git_manager = WorktreeGitManager(target_project.root_path)
    startup_git_manager = MagicMock(spec=WorktreeGitManager)
    startup_git_manager.repo_path = Path(daemon_project.root_path)
    startup_git_manager.get_current_branch.return_value = "daemon-base"
    git_manager_resolver = MagicMock(return_value=target_git_manager)
    worktree_storage = LocalWorktreeManager(temp_db)
    worktree_path = tmp_path / f"worktree-{targeting}"
    execute_spawn = AsyncMock(
        return_value=MagicMock(
            success=True,
            run_id="run-123",
            child_session_id="child-456",
            status="pending",
        )
    )

    from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

    registry = create_spawn_agent_registry(
        mock_runner,
        worktree_storage=worktree_storage,
        git_manager=startup_git_manager,
        git_manager_resolver=git_manager_resolver,
        session_manager=SessionManager(temp_db),
        db=temp_db,
    )
    arguments = {
        "prompt": "Inspect the target checkout",
        "parent_session_id": parent_session_id,
        "isolation": "worktree",
        "branch_name": f"agent/{targeting}",
    }
    if targeting == "explicit_path":
        arguments["project_path"] = target_project.root_path

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
            return_value=agent_body,
        ),
        patch.object(target_git_manager, "has_unpushed_commits", return_value=(True, 1)),
        patch.object(
            WorktreeIsolationHandler,
            "_generate_worktree_path",
            return_value=str(worktree_path),
        ),
        patch(
            "gobby.agents.isolation_worktree.repair_isolation_environment",
            new=AsyncMock(),
        ) as repair_environment,
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
            return_value=None,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            new=execute_spawn,
        ),
    ):
        result = await registry.call("spawn_agent", arguments)

    assert result["success"] is True
    git_manager_resolver.assert_called_once_with(target_project.project.id)
    startup_git_manager.get_current_branch.assert_not_called()
    assert _git(worktree_path, "rev-parse", "HEAD") == target_sha
    assert (worktree_path / "identity.txt").read_text() == "target"
    marker = json.loads((worktree_path / ".gobby" / "project.json").read_text())
    assert marker["id"] == target_project.project.id

    execute_args = execute_spawn.await_args
    assert execute_args is not None
    request = execute_args.args[0]
    assert request.cwd == str(worktree_path)
    assert request.project_id == target_project.project.id
    assert request.project_path == target_project.root_path
    assert request.code_index_preflight_mode == "best_effort"
    repair_environment.assert_awaited_once_with(
        main_repo_path=target_project.root_path,
        isolated_path=str(worktree_path),
        provider="claude",
    )
    stored_worktree = worktree_storage.get(result["worktree_id"])
    assert stored_worktree is not None
    assert stored_worktree.project_id == target_project.project.id
    assert stored_worktree.base_branch == "target-base"


@pytest.mark.asyncio
async def test_target_worktree_records_target_base_sha(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    target_project = isolated_checkout_factory(temp_db, "target-base-sha")
    target_sha = _initialize_repo(target_project, "target-base", "target")
    task = LocalTaskManager(temp_db).create_task(
        project_id=target_project.project.id,
        title="Capture target base",
        validation_criteria="The target repository SHA is recorded before agent execution.",
    )
    target_git_manager = WorktreeGitManager(target_project.root_path)
    worktree_storage = LocalWorktreeManager(temp_db)
    worktree_path = tmp_path / "base-sha-worktree"
    handler = WorktreeIsolationHandler(target_git_manager, worktree_storage)
    config = SpawnConfig(
        prompt="Capture the target base",
        task_id=task.id,
        task_title=task.title,
        task_seq_num=task.seq_num,
        branch_name="agent/base-sha",
        branch_prefix=None,
        base_branch="target-base",
        project_id=target_project.project.id,
        project_path=target_project.root_path,
        provider="claude",
        parent_session_id="parent",
    )

    with (
        patch.object(target_git_manager, "has_unpushed_commits", return_value=(True, 1)),
        patch.object(
            WorktreeIsolationHandler,
            "_generate_worktree_path",
            return_value=str(worktree_path),
        ),
        patch(
            "gobby.agents.isolation_worktree.repair_isolation_environment",
            new=AsyncMock(),
        ),
    ):
        isolation_ctx = await handler.prepare_environment(config)

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.base_commit_sha == target_sha
    assert isolation_ctx.extra["base_commit_sha"] == target_sha
    assert _git(worktree_path, "rev-parse", "HEAD") == target_sha


@pytest.mark.asyncio
async def test_clone_spawn_derives_manager_from_target_repository(
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    mock_runner: MagicMock,
    agent_body: AgentDefinitionBody,
) -> None:
    daemon_project = isolated_checkout_factory(temp_db, "daemon-clone")
    target_project = isolated_checkout_factory(temp_db, "target-clone")
    _initialize_repo(daemon_project, "daemon-base", "daemon")
    _initialize_repo(target_project, "target-base", "target")
    startup_git_manager = MagicMock(spec=WorktreeGitManager)
    startup_git_manager.repo_path = Path(daemon_project.root_path)
    target_git_manager = MagicMock(spec=WorktreeGitManager)
    target_git_manager.repo_path = Path(target_project.root_path)
    target_git_manager.get_current_branch.return_value = "target-base"
    derived_clone_manager = MagicMock()
    startup_clone_manager = MagicMock()
    clone_storage = MagicMock()
    isolation_handler = MagicMock()
    isolation_handler.prepare_environment = AsyncMock(
        return_value=IsolationContext(
            cwd=str(Path(target_project.root_path) / "clone"),
            branch_name="agent/clone",
            clone_id="clone-123",
            isolation_type="clone",
        )
    )
    isolation_handler.build_context_prompt.return_value = "Clone prompt"

    from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

    registry = create_spawn_agent_registry(
        mock_runner,
        git_manager=startup_git_manager,
        git_manager_resolver=MagicMock(return_value=target_git_manager),
        clone_storage=clone_storage,
        clone_manager=startup_clone_manager,
    )
    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
            return_value=agent_body,
        ),
        patch(
            "gobby.clones.git.CloneGitManager",
            return_value=derived_clone_manager,
        ) as clone_manager_type,
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler",
            return_value=isolation_handler,
        ) as get_handler,
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
            return_value=None,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            new=AsyncMock(
                return_value=MagicMock(
                    success=True,
                    run_id="run-123",
                    child_session_id="child-456",
                    status="pending",
                )
            ),
        ),
    ):
        result = await registry.call(
            "spawn_agent",
            {
                "prompt": "Clone the target checkout",
                "parent_session_id": "parent",
                "project_path": target_project.root_path,
                "isolation": "clone",
            },
        )

    assert result["success"] is True
    clone_manager_type.assert_called_once_with(Path(target_project.root_path))
    get_handler.assert_called_once_with(
        "clone",
        git_manager=target_git_manager,
        worktree_storage=None,
        clone_manager=derived_clone_manager,
        clone_storage=clone_storage,
    )
    assert startup_clone_manager is not derived_clone_manager


@pytest.mark.asyncio
async def test_reused_worktree_uses_target_manager_and_repo_path(
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    tmp_path: Path,
    mock_runner: MagicMock,
    agent_body: AgentDefinitionBody,
) -> None:
    daemon_project = isolated_checkout_factory(temp_db, "daemon-reuse")
    target_project = isolated_checkout_factory(temp_db, "target-reuse")
    _initialize_repo(daemon_project, "daemon-base", "daemon")
    _initialize_repo(target_project, "target-base", "target")
    existing_path = tmp_path / "existing-target-worktree"
    existing_path.mkdir()
    existing_worktree = SimpleNamespace(
        id="worktree-123",
        worktree_path=str(existing_path),
        branch_name="agent/reused",
    )
    worktree_storage = MagicMock()
    worktree_storage.get.return_value = existing_worktree
    startup_git_manager = MagicMock(spec=WorktreeGitManager)
    startup_git_manager.repo_path = Path(daemon_project.root_path)
    target_git_manager = MagicMock(spec=WorktreeGitManager)
    target_git_manager.repo_path = Path(target_project.root_path)
    target_git_manager.get_current_branch.return_value = "target-base"
    reuse_handler = MagicMock()
    reused_context = IsolationContext(
        cwd=str(existing_path),
        branch_name="agent/reused",
        worktree_id=existing_worktree.id,
        isolation_type="worktree",
        extra={"main_repo_path": target_project.root_path, "reused_worktree": True},
    )
    prepare_reused = AsyncMock(return_value=(reused_context, reuse_handler))

    from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

    registry = create_spawn_agent_registry(
        mock_runner,
        worktree_storage=worktree_storage,
        git_manager=startup_git_manager,
        git_manager_resolver=MagicMock(return_value=target_git_manager),
    )
    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
            return_value=agent_body,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_reused_worktree",
            new=prepare_reused,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
            return_value=None,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            new=AsyncMock(
                return_value=MagicMock(
                    success=True,
                    run_id="run-123",
                    child_session_id="child-456",
                    status="pending",
                )
            ),
        ),
    ):
        result = await registry.call(
            "spawn_agent",
            {
                "prompt": "Reuse the target worktree",
                "parent_session_id": "parent",
                "project_path": target_project.root_path,
                "worktree_id": existing_worktree.id,
            },
        )

    assert result["success"] is True
    prepare_reused.assert_awaited_once()
    prepare_args = prepare_reused.await_args
    assert prepare_args is not None
    prepare_kwargs = prepare_args.kwargs
    assert prepare_kwargs["git_manager"] is target_git_manager
    assert prepare_kwargs["main_repo_path"] == target_project.root_path
    assert prepare_kwargs["spawn_config"].base_branch == "target-base"
    startup_git_manager.get_current_branch.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_fails_closed_when_target_checkout_is_unresolved(
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
    mock_runner: MagicMock,
    agent_body: AgentDefinitionBody,
) -> None:
    target_project = isolated_checkout_factory(temp_db, "target-missing-checkout")
    parent_session_id = _register_parent(temp_db, target_project, "parent-missing-checkout")
    LocalProjectCheckoutManager(temp_db).unregister_project(target_project.project.id)
    startup_git_manager = MagicMock(spec=WorktreeGitManager)
    startup_git_manager.repo_path = Path.cwd()
    resolver = MagicMock(return_value=None)

    from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

    registry = create_spawn_agent_registry(
        mock_runner,
        git_manager=startup_git_manager,
        git_manager_resolver=resolver,
        session_manager=SessionManager(temp_db),
        db=temp_db,
    )
    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
        return_value=agent_body,
    ):
        result = await registry.call(
            "spawn_agent",
            {
                "prompt": "Do not fall back",
                "parent_session_id": parent_session_id,
                "isolation": "worktree",
            },
        )

    assert result == {
        "success": False,
        "error": f"No Git manager available for project '{target_project.project.id}'",
    }
    resolver.assert_called_once_with(target_project.project.id)
    startup_git_manager.get_current_branch.assert_not_called()
