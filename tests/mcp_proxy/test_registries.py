from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.registries import setup_internal_registries

pytestmark = pytest.mark.unit


def test_setup_internal_registries_with_merge() -> None:
    merge_storage = MagicMock()
    merge_resolver = MagicMock()
    git_manager = MagicMock()
    worktree_manager = MagicMock()

    manager = setup_internal_registries(
        _config=MagicMock(),
        merge_storage=merge_storage,
        merge_resolver=merge_resolver,
        git_manager=git_manager,
        worktree_storage=worktree_manager,
    )

    registries = manager.get_all_registries()
    assert any(r.name == "gobby-merge" for r in registries)


def test_setup_with_config_none() -> None:
    """Test setup with config=None disables tasks registry."""
    manager = setup_internal_registries(_config=None)

    registries = manager.get_all_registries()
    # Tasks registry should NOT be present when config is None
    assert not any(r.name == "gobby-tasks" for r in registries)
    # Workflows registry is always present
    assert any(r.name == "gobby-workflows" for r in registries)


def test_setup_with_all_managers_none() -> None:
    """Test setup with all optional managers as None."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(
        _config=mock_config,
        memory_manager=None,
        task_manager=None,
        sync_manager=None,
        message_manager=None,
        local_session_manager=None,
        metrics_manager=None,
        agent_runner=None,
        worktree_storage=None,
        merge_storage=None,
        merge_resolver=None,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    # Always-on registries should be present
    assert "gobby-workflows" in registry_names
    # These should NOT be present when their managers are None
    assert "gobby-memory" not in registry_names
    assert "gobby-metrics" not in registry_names
    assert "gobby-agents" not in registry_names
    assert "gobby-worktrees" not in registry_names
    assert "gobby-merge" not in registry_names


def test_setup_with_memory_manager_only() -> None:
    """Test setup with only memory manager enabled."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    memory_manager = MagicMock()

    manager = setup_internal_registries(
        _config=mock_config,
        memory_manager=memory_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-memory" in registry_names
    assert "gobby-workflows" in registry_names


def test_setup_with_metrics_manager_only() -> None:
    """Test setup with only metrics manager enabled."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    metrics_manager = MagicMock()

    manager = setup_internal_registries(
        _config=mock_config,
        metrics_manager=metrics_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-metrics" in registry_names


def test_setup_with_agent_runner_only() -> None:
    """Test setup with only agent runner enabled."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    agent_runner = MagicMock()

    manager = setup_internal_registries(
        _config=mock_config,
        agent_runner=agent_runner,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-agents" in registry_names


def test_setup_with_worktree_storage_only() -> None:
    """Test setup with only worktree storage enabled."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    worktree_storage = MagicMock()

    manager = setup_internal_registries(
        _config=mock_config,
        worktree_storage=worktree_storage,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-worktrees" in registry_names


def test_setup_sessions_with_message_manager() -> None:
    """Test sessions registry is created with message_manager."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    message_manager = MagicMock()

    manager = setup_internal_registries(
        _config=mock_config,
        message_manager=message_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-sessions" in registry_names


def test_setup_sessions_with_local_session_manager() -> None:
    """Test sessions registry is created with local_session_manager."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    local_session_manager = MagicMock()

    manager = setup_internal_registries(
        _config=mock_config,
        local_session_manager=local_session_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-sessions" in registry_names


def test_setup_hub_registry_with_database_path() -> None:
    """Test hub registry is created when config has database_path."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    mock_config.database_path = "/tmp/test.db"

    manager = setup_internal_registries(_config=mock_config)

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-hub" in registry_names


def test_setup_tasks_disabled_by_config() -> None:
    """Test tasks registry is not created when disabled in config."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    task_manager = MagicMock()
    sync_manager = MagicMock()

    manager = setup_internal_registries(
        _config=mock_config,
        task_manager=task_manager,
        sync_manager=sync_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    # Tasks should NOT be present when disabled in config
    assert "gobby-tasks" not in registry_names


def test_setup_tasks_missing_task_manager() -> None:
    """Test tasks registry is not created when task_manager is None."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = True
    sync_manager = MagicMock()

    manager = setup_internal_registries(
        _config=mock_config,
        task_manager=None,
        sync_manager=sync_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    # Tasks should NOT be present when task_manager is None
    assert "gobby-tasks" not in registry_names


def test_setup_tasks_missing_sync_manager() -> None:
    """Test tasks registry is not created when sync_manager is None."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = True
    task_manager = MagicMock()

    manager = setup_internal_registries(
        _config=mock_config,
        task_manager=task_manager,
        sync_manager=None,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    # Tasks should NOT be present when sync_manager is None
    assert "gobby-tasks" not in registry_names


def test_setup_merge_requires_both_storage_and_resolver() -> None:
    """Test merge registry requires both merge_storage and merge_resolver."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    # Test with only storage
    manager1 = setup_internal_registries(
        _config=mock_config,
        merge_storage=MagicMock(),
        merge_resolver=None,
    )
    registries1 = [r.name for r in manager1.get_all_registries()]
    assert "gobby-merge" not in registries1

    # Test with only resolver
    manager2 = setup_internal_registries(
        _config=mock_config,
        merge_storage=None,
        merge_resolver=MagicMock(),
    )
    registries2 = [r.name for r in manager2.get_all_registries()]
    assert "gobby-merge" not in registries2


# --- Skills Registry Tests ---


def test_setup_with_database_path(tmp_path) -> None:
    """Test registries are created when config has database_path."""
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations

    # Create a real test database with migrations applied
    db_path = tmp_path / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)

    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    mock_config.database_path = str(db_path)

    manager = setup_internal_registries(_config=mock_config)

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    # Core registries should always be present
    assert "gobby-workflows" in registry_names
    assert "gobby-hub" in registry_names


def test_setup_skills_registry_not_created_without_config() -> None:
    """Test skills registry is not created when config is None."""
    manager = setup_internal_registries(_config=None)

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-skills" not in registry_names


def test_setup_skills_registry_not_created_without_database_path() -> None:
    """Test skills registry is not created when database_path is missing."""
    mock_config = MagicMock(spec=["get_gobby_tasks_config"])
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    # Note: mock_config does NOT have database_path attribute

    manager = setup_internal_registries(_config=mock_config)

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-skills" not in registry_names


def test_setup_hub_registry_has_expected_tools(tmp_path) -> None:
    """Test hub registry has expected tools."""
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations

    # Create a real test database with migrations applied
    db_path = tmp_path / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)

    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    mock_config.database_path = str(db_path)

    manager = setup_internal_registries(_config=mock_config)

    # Find the hub registry
    hub_registry = None
    for registry in manager.get_all_registries():
        if registry.name == "gobby-hub":
            hub_registry = registry
            break

    assert hub_registry is not None

    # Verify some expected tools are registered
    tool_names = list(hub_registry._tools.keys())
    # Hub registry should have at least some tools
    assert len(tool_names) > 0


def test_setup_hub_registry_accepts_project_id(tmp_path) -> None:
    """Test hub registry accepts project_id when provided."""
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations

    # Create a real test database
    db_path = tmp_path / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)

    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    mock_config.database_path = str(db_path)

    # Create a project in the database for foreign key constraint
    project_id = "test-project-123"
    db.execute(
        "INSERT INTO projects (id, name, repo_path, github_url, created_at, updated_at) VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (project_id, "Test Project", "/tmp/test", None),
    )

    manager = setup_internal_registries(
        _config=mock_config,
        project_id=project_id,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    # Hub registry should be present
    assert "gobby-hub" in registry_names


# --- Pipelines Registry Tests ---


def test_setup_with_pipeline_executor() -> None:
    """Test pipelines registry is created when pipeline_executor is provided."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(
        _config=mock_config,
        pipeline_executor=MagicMock(),
        workflow_loader=MagicMock(),
        pipeline_execution_manager=MagicMock(),
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-workflows" in registry_names


def test_setup_pipelines_always_registered_even_without_executor() -> None:
    """Test pipelines registry is always created, even when executor is None."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(
        _config=mock_config,
        pipeline_executor=None,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-workflows" in registry_names


def test_setup_pipelines_tools_accessible() -> None:
    """Test that pipelines tools are accessible via registry."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(
        _config=mock_config,
        pipeline_executor=MagicMock(),
        workflow_loader=MagicMock(),
        pipeline_execution_manager=MagicMock(),
    )

    # Find the pipelines registry
    pipelines_registry = None
    for registry in manager.get_all_registries():
        if registry.name == "gobby-workflows":
            pipelines_registry = registry
            break

    assert pipelines_registry is not None

    # Verify expected tools are registered
    tool_names = [t["name"] for t in pipelines_registry.list_tools()]
    assert "list_pipelines" in tool_names
    assert "run_pipeline" in tool_names
    assert "get_pipeline_status" in tool_names
    assert "approve_pipeline" in tool_names
    assert "reject_pipeline" in tool_names


# --- Clones Registry Tests ---


def test_setup_clones_registered_without_git_manager() -> None:
    """Test clones registry is created even when git_manager is None."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(
        _config=mock_config,
        clone_storage=MagicMock(),
        git_manager=None,
        project_id="test-project",
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-clones" in registry_names


def test_setup_clones_registered_with_git_manager() -> None:
    """Test clones registry is created when git_manager is available."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    mock_git = MagicMock()
    mock_git.repo_path = "/tmp/test-repo"

    manager = setup_internal_registries(
        _config=mock_config,
        clone_storage=MagicMock(),
        git_manager=mock_git,
        project_id="test-project",
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-clones" in registry_names


def test_setup_pipelines_tools_accessible_without_executor() -> None:
    """Test that pipelines tools are listed even when executor is None."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(
        _config=mock_config,
        pipeline_executor=None,
        workflow_loader=None,
    )

    pipelines_registry = None
    for registry in manager.get_all_registries():
        if registry.name == "gobby-workflows":
            pipelines_registry = registry
            break

    assert pipelines_registry is not None
    tool_names = [t["name"] for t in pipelines_registry.list_tools()]
    assert "list_pipelines" in tool_names
    assert "run_pipeline" in tool_names
