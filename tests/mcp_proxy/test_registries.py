from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.config.features import ToolResultOffloadConfig
from gobby.mcp_proxy import registries as registries_module
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.tools.skills import create_skills_registry
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db(hub_db: Any) -> Any:
    return hub_db


def test_setup_internal_registries_with_merge() -> None:
    merge_storage = MagicMock()
    merge_resolver = MagicMock()
    git_manager = MagicMock()
    worktree_manager = MagicMock()

    manager = setup_internal_registries(
        config_resolver=lambda: MagicMock(),
        merge_storage=merge_storage,
        merge_resolver=merge_resolver,
        git_manager=git_manager,
        worktree_storage=worktree_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-merge" in registry_names
    assert "gobby-worktrees" in registry_names


def test_setup_with_config_none() -> None:
    """Test setup with config=None disables tasks registry."""
    manager = setup_internal_registries(config_resolver=lambda: None)

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
        config_resolver=lambda: mock_config,
        memory_manager_resolver=None,
        task_manager=None,
        session_manager=None,
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
    assert "gobby-artifacts" not in registry_names
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
        config_resolver=lambda: mock_config,
        memory_manager_resolver=lambda: memory_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-memory" in registry_names
    assert "gobby-workflows" in registry_names


@pytest.mark.asyncio
async def test_memory_registry_recovers_after_runtime_manager_rebuild() -> None:
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    current: list[Any | None] = [None]

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
        memory_manager_resolver=lambda: current[0],
    )
    registry = manager.get_registry("gobby-memory")
    assert registry is not None

    rebuilt = MagicMock()
    rebuilt.get_stats = AsyncMock(return_value={"total": 1})
    current[0] = rebuilt
    with patch(
        "gobby.utils.project_context.get_project_context",
        return_value={"id": "11111111-1111-4111-8111-111111110001"},
    ):
        result = await registry.call("memory_stats", {})

    assert result["stats"]["total"] == 1
    rebuilt.get_stats.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_learning_registry_recovers_after_runtime_manager_rebuild() -> None:
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    current: list[Any | None] = [None]

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
        memory_manager_resolver=lambda: current[0],
        task_manager=MagicMock(),
    )
    registry = manager.get_registry("gobby-review-learning")
    assert registry is not None

    rebuilt = MagicMock()
    rebuilt.search_memories = AsyncMock(return_value=[])
    current[0] = rebuilt
    # service.py from-imports both helpers, so patch the bindings it uses;
    # a live GOBBY_SESSION_ID would otherwise route scope resolution at the DB.
    with (
        patch(
            "gobby.review_learning.service.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ),
        patch(
            "gobby.review_learning.service.get_current_session_id",
            return_value=None,
        ),
    ):
        result = await registry.call(
            "recall_review_context",
            {"findings": ["runtime service was rebuilt"]},
        )

    assert result["success"] is True
    rebuilt.search_memories.assert_awaited()


def test_setup_with_metrics_manager_only() -> None:
    """Test setup with only metrics manager enabled."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    metrics_manager = MagicMock()

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
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
        config_resolver=lambda: mock_config,
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
        config_resolver=lambda: mock_config,
        worktree_storage=worktree_storage,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-worktrees" in registry_names


@pytest.mark.asyncio
@pytest.mark.parametrize("session_ref_kind", ["hash", "uuid"], ids=["hash-ref", "uuid-ref"])
async def test_setup_worktrees_registry_claim_resolves_session_refs(
    temp_db: Any,
    project_manager: Any,
    session_manager: Any,
    session_ref_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worktree registry wiring resolves shorthand and UUID session refs."""
    monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    worktree_storage = LocalWorktreeManager(temp_db)
    project = project_manager.create(name="test-project", repo_path="/tmp/test-project")
    session = session_manager.register(
        machine_id=None,
        source="codex",
        project_id=project.id,
        external_id="ext-test-session",
        title="Test Session",
    )
    worktree = worktree_storage.create(
        project_id=project.id,
        branch_name=f"feature/{session_ref_kind}",
        worktree_path=f"/tmp/worktrees/{session_ref_kind}",
    )

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
        session_manager=session_manager,
        worktree_storage=worktree_storage,
        project_id=project.id,
    )

    registry = manager.get_registry("gobby-worktrees")
    assert registry is not None

    session_ref = f"#{session.seq_num}" if session_ref_kind == "hash" else session.id
    result = await registry.call(
        "claim_worktree",
        {"worktree_id": worktree.id, "session_id": session_ref},
    )

    assert result["success"] is True
    claimed = worktree_storage.get(worktree.id)
    assert claimed is not None
    assert claimed.agent_session_id == session.id


def test_setup_sessions_with_session_manager() -> None:
    """Test sessions registry is created with session_manager."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    session_manager = MagicMock()

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
        session_manager=session_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-sessions" in registry_names


def test_setup_sessions_registry_forwards_memory_manager_resolver() -> None:
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    memory_manager_resolver = MagicMock(return_value=None)

    with patch(
        "gobby.mcp_proxy.tools.sessions.create_session_messages_registry"
    ) as create_sessions_registry:
        create_sessions_registry.return_value.name = "gobby-sessions"
        manager = setup_internal_registries(
            config_resolver=lambda: mock_config,
            session_manager=MagicMock(),
            memory_manager_resolver=memory_manager_resolver,
        )

    assert manager.get_registry("gobby-sessions") is create_sessions_registry.return_value
    registry_names = [registry.name for registry in manager.get_all_registries()]
    assert "gobby-sessions" in registry_names
    assert "gobby-memory" in registry_names
    assert create_sessions_registry.call_args.kwargs["memory_manager_resolver"] is (
        memory_manager_resolver
    )


def test_setup_hub_registry_with_active_database(temp_db: Any) -> None:
    """Test hub registry is created from the active runtime database."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(config_resolver=lambda: mock_config, db=temp_db)

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-hub" in registry_names


def test_setup_hub_registry_not_created_without_active_database() -> None:
    """Test hub registry is not created without an active runtime database."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(config_resolver=lambda: mock_config)

    registry_names = [r.name for r in manager.get_all_registries()]
    assert "gobby-hub" not in registry_names


def test_setup_results_registry_requires_database_and_enabled_config(temp_db: Any) -> None:
    enabled_config = MagicMock()
    enabled_config.get_gobby_tasks_config.return_value.enabled = False
    enabled_config.get_tool_result_offload_config.return_value = ToolResultOffloadConfig()

    without_database = setup_internal_registries(config_resolver=lambda: enabled_config)
    with_database = setup_internal_registries(config_resolver=lambda: enabled_config, db=temp_db)

    assert without_database.get_registry("gobby-results") is None
    assert with_database.get_registry("gobby-results") is not None


def test_setup_results_registry_omitted_when_offload_disabled(temp_db: Any) -> None:
    config = MagicMock()
    config.get_gobby_tasks_config.return_value.enabled = False
    config.get_tool_result_offload_config.return_value = ToolResultOffloadConfig(enabled=False)

    manager = setup_internal_registries(config_resolver=lambda: config, db=temp_db)

    assert manager.get_registry("gobby-results") is None


def test_setup_tasks_disabled_by_config() -> None:
    """Test tasks registry is not created when disabled in config."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    task_manager = MagicMock()

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
        task_manager=task_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    # Tasks should NOT be present when disabled in config
    assert "gobby-tasks" not in registry_names


def test_setup_plans_registry_when_db_exists_even_without_tasks(temp_db: Any) -> None:
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(config_resolver=lambda: mock_config, db=temp_db)

    registry_names = [r.name for r in manager.get_all_registries()]
    assert "gobby-plans" in registry_names


def test_setup_tasks_missing_task_manager() -> None:
    """Test tasks registry is not created when task_manager is None."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = True

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
        task_manager=None,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    # Tasks should NOT be present when task_manager is None
    assert "gobby-tasks" not in registry_names


def test_setup_tasks_only_requires_task_manager() -> None:
    """Test task registries do not depend on a legacy sync manager."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = True
    task_manager = MagicMock()

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
        task_manager=task_manager,
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-tasks" in registry_names
    assert "gobby-tasks-ops" in registry_names


def test_setup_tasks_ops_registry_omits_legacy_front_half_tick(temp_db: Any) -> None:
    """The stage-native build flow no longer exposes front_half_tick."""
    from gobby.storage.tasks import LocalTaskManager

    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = True

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
        task_manager=LocalTaskManager(temp_db),
    )

    registry = manager.get_registry("gobby-tasks-ops")
    assert registry is not None

    tool_names = [tool["name"] for tool in registry.list_tools()]
    assert "front_half_tick" not in tool_names
    assert "build_task" in tool_names


def test_setup_merge_requires_both_storage_and_resolver() -> None:
    """Test merge registry requires both merge_storage and merge_resolver."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    # Test with only storage
    manager1 = setup_internal_registries(
        config_resolver=lambda: mock_config,
        merge_storage=MagicMock(),
        merge_resolver=None,
    )
    registries1 = [r.name for r in manager1.get_all_registries()]
    assert "gobby-merge" not in registries1

    # Test with only resolver
    manager2 = setup_internal_registries(
        config_resolver=lambda: mock_config,
        merge_storage=None,
        merge_resolver=MagicMock(),
    )
    registries2 = [r.name for r in manager2.get_all_registries()]
    assert "gobby-merge" not in registries2


# --- Skills Registry Tests ---


def test_setup_with_active_database(hub_db: Any) -> None:
    """Test registries are created when an active database is provided."""
    db = hub_db

    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(config_resolver=lambda: mock_config, db=db)

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    # Core registries should always be present
    assert "gobby-workflows" in registry_names
    assert "gobby-hub" in registry_names


def test_skills_cache_resolvers_capture_config_while_holding_cache_lock() -> None:
    class RecordingLock:
        def __init__(self) -> None:
            self.held = False

        def __enter__(self) -> "RecordingLock":
            assert self.held is False
            self.held = True
            return self

        def __exit__(self, *args: object) -> None:
            self.held = False

    locks: list[RecordingLock] = []

    def lock_factory() -> RecordingLock:
        lock = RecordingLock()
        locks.append(lock)
        return lock

    observed_lock_states: list[tuple[bool, ...]] = []
    config = MagicMock()
    config.get_gobby_tasks_config.return_value.enabled = False

    def resolve_config() -> Any:
        observed_lock_states.append(tuple(lock.held for lock in locks))
        return config

    with (
        patch.object(registries_module, "threading", SimpleNamespace(Lock=lock_factory)),
        patch(
            "gobby.mcp_proxy.tools.skills.create_skills_registry",
            wraps=create_skills_registry,
        ) as create_registry,
    ):
        setup_internal_registries(config_resolver=resolve_config, db=MagicMock())

    assert len(locks) == 2
    kwargs = create_registry.call_args.kwargs
    kwargs["hub_manager_resolver"]()
    kwargs["search_resolver"]()

    assert observed_lock_states[-2:] == [(True, False), (False, True)]


def test_setup_skills_registry_not_created_without_config() -> None:
    """Test skills registry is not created when config is None."""
    manager = setup_internal_registries(config_resolver=lambda: None)

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-skills" not in registry_names


def test_setup_skills_registry_not_created_without_database() -> None:
    """Test skills registry is not created when database is missing."""
    mock_config = MagicMock(spec=["get_gobby_tasks_config"])
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(config_resolver=lambda: mock_config)

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-skills" not in registry_names


def test_setup_hub_registry_has_expected_tools(hub_db: Any) -> None:
    """Test hub registry has expected tools."""
    db = hub_db

    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(config_resolver=lambda: mock_config, db=db)

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


def test_setup_hub_registry_accepts_project_id(hub_db: Any) -> None:
    """Test hub registry accepts project_id when provided."""
    db = hub_db

    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False
    # Create a project in the database for foreign key constraint
    project_id = str(uuid4())
    db.execute(
        "INSERT INTO projects (id, name, repo_path, github_url, created_at, updated_at) VALUES (%s, %s, %s, %s, NOW(), NOW())",
        (project_id, "Test Project", "/tmp/test", None),
    )

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
        db=db,
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
        config_resolver=lambda: mock_config,
        pipeline_executor=MagicMock(),
        workflow_loader=MagicMock(),
        pipeline_execution_manager=MagicMock(),
    )

    registries = manager.get_all_registries()
    registry_names = [r.name for r in registries]
    assert "gobby-workflows" in registry_names
    assert len(registry_names) >= 1


def test_setup_pipelines_always_registered_even_without_executor() -> None:
    """Test pipelines registry is always created, even when executor is None."""
    mock_config = MagicMock()
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    manager = setup_internal_registries(
        config_resolver=lambda: mock_config,
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
        config_resolver=lambda: mock_config,
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
        config_resolver=lambda: mock_config,
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
        config_resolver=lambda: mock_config,
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
        config_resolver=lambda: mock_config,
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


class TestHubApiKeyResolution:
    """Hub auth at the registries layer resolves from SecretStore, never env."""

    def _build_skills_config(self) -> Any:
        from gobby.config.skills import HubConfig, SkillsConfig

        return SkillsConfig(
            hubs={
                "skillsmp": HubConfig(
                    type="skillsmp",
                    base_url="https://skillsmp.com/api/v1",
                    auth_key_name="SKILLSMP_API_KEY",
                ),
                "clawdhub": HubConfig(type="clawdhub"),  # no auth
            }
        )

    def _run_setup_with_captured_hub_manager(self, db: Any, skills_config: Any) -> dict[str, Any]:
        """Invoke setup_internal_registries with a sentinel HubManager to capture kwargs."""
        from unittest.mock import patch as patch_fn

        from gobby.skills.hubs.manager import HubManager

        captured: dict[str, Any] = {}

        class RecordingHubManager(HubManager):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured["args"] = args
                captured["kwargs"] = kwargs
                super().__init__(*args, **kwargs)

        mock_config = MagicMock()
        mock_config.skills = skills_config
        mock_config.embeddings = None
        mock_config.skill_description = None
        mock_config.get_gobby_tasks_config.return_value.enabled = False
        mock_config.get_search_config.return_value = None

        # Patch at the source module — registries.py imports HubManager inside
        # the function body, so it resolves via gobby.skills.hubs at call time.
        with patch_fn("gobby.skills.hubs.HubManager", RecordingHubManager):
            setup_internal_registries(config_resolver=lambda: mock_config, db=db)

        return captured

    def test_hub_api_key_resolution_ignores_environment(
        self, hub_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env vars are never consulted for hub auth — only SecretStore."""
        db = hub_db
        try:
            # Env has a value but SecretStore does NOT.
            monkeypatch.setenv("SKILLSMP_API_KEY", "env-bogus-should-be-ignored")

            captured = self._run_setup_with_captured_hub_manager(db, self._build_skills_config())

            api_keys = captured["kwargs"]["api_keys"]
            assert "SKILLSMP_API_KEY" not in api_keys
        finally:
            db.close()

    def test_hub_api_key_resolution_reads_secret_store(self, hub_db: Any) -> None:
        """When a secret is stored in SecretStore, the HubManager receives it."""
        from gobby.storage.secrets import SecretStore

        db = hub_db
        try:
            SecretStore(db).set(
                name="SKILLSMP_API_KEY",
                plaintext_value="stored-secret-value",
                category="integration",
                description="test",
            )

            captured = self._run_setup_with_captured_hub_manager(db, self._build_skills_config())

            api_keys = captured["kwargs"]["api_keys"]
            assert api_keys["SKILLSMP_API_KEY"] == "stored-secret-value"
            # clawdhub has no auth_key_name so it must not leak a key in api_keys.
            assert len(api_keys) == 1
        finally:
            db.close()


def test_terminal_tools_receive_composition_root_services() -> None:
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.write_coordinator import WriteCoordinator
    from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal

    store = MemoryTerminalStore(make_memory_terminal())
    runtime = FakeRuntime()
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    coordinator = WriteCoordinator(store, runtime)
    captured: dict[str, object] = {}

    def fake_create_session_messages_registry(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(name="gobby-sessions")

    with patch(
        "gobby.mcp_proxy.tools.sessions.create_session_messages_registry",
        fake_create_session_messages_registry,
    ):
        setup_internal_registries(
            config_resolver=lambda: MagicMock(),
            session_manager=MagicMock(),
            db=MagicMock(),
            terminal_manager=store,
            terminal_runtime_registry=registry,
            write_coordinator=coordinator,
        )
    assert captured["terminal_manager"] is store
    assert captured["terminal_runtime_registry"] is registry
    assert captured["write_coordinator"] is coordinator
