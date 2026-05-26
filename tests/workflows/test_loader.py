"""Comprehensive tests for WorkflowLoader (DB-only runtime)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import PipelineDefinition, WorkflowDefinition
from gobby.workflows.loader import WorkflowLoader
from gobby.workflows.loader_cache import DiscoveredWorkflow

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a fresh database with migrations applied."""
    database = temp_db
    return database


@pytest.fixture(autouse=True)
def _clean_bundled_workflows(db: HubDatabase) -> None:
    """Remove bundled workflows imported by migrations so tests start clean."""
    db.execute("DELETE FROM workflow_definitions WHERE source = 'template'")


@pytest.fixture
def project(db: HubDatabase) -> Project:
    """Create a test project for FK-safe project-scoped workflow tests."""
    pm = LocalProjectManager(db)
    return pm.create(name="test-project", repo_path="/test/project")


@pytest.fixture
def def_manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    """Create a workflow definition manager backed by the test database."""
    return LocalWorkflowDefinitionManager(db)


@pytest.fixture
def loader(db: HubDatabase) -> WorkflowLoader:
    """Create a WorkflowLoader backed by the test database."""
    return WorkflowLoader(db=db)


# ---------------------------------------------------------------------------
# TestWorkflowLoader -- basic load / cache behaviour
# ---------------------------------------------------------------------------


class TestWorkflowLoader:
    """Tests for WorkflowLoader basic functionality."""

    def test_init_default_dirs(self) -> None:
        """Test default workflow directory initialization (legacy param)."""
        loader = WorkflowLoader()
        assert len(loader.global_dirs) == 1
        assert loader.global_dirs[0] == Path.home() / ".gobby" / "workflows"

    def test_init_custom_dirs(self) -> None:
        """Test custom workflow directories initialization (legacy param)."""
        custom_dirs = [Path("/custom/path1"), Path("/custom/path2")]
        loader = WorkflowLoader(workflow_dirs=custom_dirs)
        assert loader.global_dirs == custom_dirs

    def test_init_accepts_db_param(self, db: HubDatabase) -> None:
        """Test that WorkflowLoader accepts a db parameter."""
        loader = WorkflowLoader(db=db)
        assert loader.db is db

    def test_init_without_db(self) -> None:
        """Test that WorkflowLoader works without a db parameter."""
        loader = WorkflowLoader()
        assert loader.db is None

    @pytest.mark.asyncio
    async def test_load_workflow_not_found(self, loader: WorkflowLoader) -> None:
        """Test loading a workflow that does not exist returns None."""
        result = await loader.load_workflow("non_existent")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_workflow_from_db(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test loading a valid workflow definition from the database."""
        definition_data = {
            "name": "test_workflow",
            "version": "1.0.0",
            "steps": [{"name": "step1", "allowed_tools": "all"}],
        }
        def_manager.create(
            name="test_workflow",
            definition_json=json.dumps(definition_data),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("test_workflow")

        assert wf is not None
        assert wf.name == "test_workflow"
        assert len(wf.steps) == 1
        assert wf.steps[0].name == "step1"

    @pytest.mark.asyncio
    async def test_load_workflow_invalid_json(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test loading a workflow with an invalid JSON shape returns None."""
        def_manager.create(
            name="invalid",
            definition_json=json.dumps("bad workflow payload"),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("invalid")
        assert wf is None

    @pytest.mark.asyncio
    async def test_load_workflow_exception_handling(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that parse errors during loading return None."""
        # Insert data that will fail WorkflowDefinition parsing (missing name)
        def_manager.create(
            name="bad_parse",
            definition_json=json.dumps({"steps": "not-a-list"}),
            workflow_type="workflow",
        )

        result = await loader.load_workflow("bad_parse")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_workflow_with_project_id(
        self,
        db: HubDatabase,
        project: Project,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that project-scoped workflows are found via project_path."""
        definition_data = {
            "name": "project_wf",
            "version": "1.0.0",
            "steps": [{"name": "s1", "allowed_tools": "all"}],
        }
        def_manager.create(
            name="project_wf",
            definition_json=json.dumps(definition_data),
            workflow_type="workflow",
            project_id=project.id,
        )

        loader = WorkflowLoader(db=db)
        wf = await loader.load_workflow("project_wf", project_path=project.id)

        assert wf is not None
        assert wf.name == "project_wf"

    @pytest.mark.asyncio
    async def test_load_workflow_caching(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that loaded workflows are cached (same object identity)."""
        definition_data = {
            "name": "cached_workflow",
            "version": "1.0.0",
            "steps": [{"name": "step1", "allowed_tools": "all"}],
        }
        def_manager.create(
            name="cached_workflow",
            definition_json=json.dumps(definition_data),
            workflow_type="workflow",
        )

        wf1 = await loader.load_workflow("cached_workflow")
        wf2 = await loader.load_workflow("cached_workflow")

        assert wf1 is not None
        assert wf2 is not None
        assert wf1 is wf2

    @pytest.mark.asyncio
    async def test_load_workflow_cache_key_includes_project(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that cache keys include project path for proper isolation."""
        definition_data = {
            "name": "project_workflow",
            "version": "1.0.0",
            "steps": [{"name": "step1", "allowed_tools": "all"}],
        }
        # Create a global entry
        def_manager.create(
            name="project_workflow",
            definition_json=json.dumps(definition_data),
            workflow_type="workflow",
        )

        # Load without project path
        await loader.load_workflow("project_workflow")
        # Load with project path
        await loader.load_workflow("project_workflow", project_path="/project/a")

        assert "global:project_workflow" in loader._cache
        assert "/project/a:project_workflow" in loader._cache

    @pytest.mark.asyncio
    async def test_clear_cache_forces_reload(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that clearing cache forces reload from the database."""
        definition_v1 = {
            "name": "dynamic_workflow",
            "version": "1.0.0",
            "steps": [{"name": "step1", "allowed_tools": "all"}],
        }
        row = def_manager.create(
            name="dynamic_workflow",
            definition_json=json.dumps(definition_v1),
            workflow_type="workflow",
        )

        # First load
        wf1 = await loader.load_workflow("dynamic_workflow")
        assert wf1 is not None
        assert wf1.version == "1.0.0"

        # Update the definition in the DB
        definition_v2 = {
            "name": "dynamic_workflow",
            "version": "2.0",
            "steps": [{"name": "step1", "allowed_tools": "all"}],
        }
        def_manager.update(row.id, definition_json=json.dumps(definition_v2))

        # Cache hit should still return v1
        wf_cached = await loader.load_workflow("dynamic_workflow")
        assert wf_cached.version == "1.0.0"

        # Clear cache
        loader.clear_cache()

        # Should now reload v2 from DB
        wf2 = await loader.load_workflow("dynamic_workflow")
        assert wf2 is not None
        assert wf2.version == "2.0"


# ---------------------------------------------------------------------------
# TestWorkflowInheritance -- extends resolution via DB
# ---------------------------------------------------------------------------


class TestWorkflowInheritance:
    """Tests for workflow inheritance via DB-backed extends resolution."""

    @pytest.mark.asyncio
    async def test_valid_inheritance(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that valid inheritance (child extends parent) works correctly."""
        parent_data = {
            "name": "parent_workflow",
            "version": "1.0.0",
            "steps": [{"name": "step1", "allowed_tools": "all"}],
        }
        child_data = {
            "name": "child_workflow",
            "version": "1.0.0",
            "extends": "parent_workflow",
            "steps": [{"name": "step2", "allowed_tools": ["read", "write"]}],
        }
        def_manager.create(
            name="parent_workflow",
            definition_json=json.dumps(parent_data),
            workflow_type="workflow",
        )
        def_manager.create(
            name="child_workflow",
            definition_json=json.dumps(child_data),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("child_workflow")

        assert wf is not None
        assert wf.name == "child_workflow"
        step_names = [s.name for s in wf.steps]
        assert "step1" in step_names
        assert "step2" in step_names

    @pytest.mark.asyncio
    async def test_parent_workflow_not_found(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test handling when the parent workflow does not exist in DB."""
        child_data = {
            "name": "orphan_workflow",
            "version": "1.0.0",
            "extends": "nonexistent_parent",
            "steps": [{"name": "step1", "allowed_tools": "all"}],
        }
        def_manager.create(
            name="orphan_workflow",
            definition_json=json.dumps(child_data),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("orphan_workflow")

        # Should still load (with warning), just without parent merge
        assert wf is not None
        assert wf.name == "orphan_workflow"

    @pytest.mark.asyncio
    async def test_valid_chain_inheritance(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that valid chain (A extends B extends C) works correctly."""
        base_data = {
            "name": "base",
            "version": "1.0.0",
            "steps": [{"name": "base_step", "allowed_tools": "all"}],
        }
        middle_data = {
            "name": "middle",
            "version": "1.0.0",
            "extends": "base",
            "steps": [{"name": "middle_step", "allowed_tools": ["read"]}],
        }
        top_data = {
            "name": "top",
            "version": "1.0.0",
            "extends": "middle",
            "steps": [{"name": "top_step", "allowed_tools": ["write"]}],
        }
        def_manager.create(
            name="base",
            definition_json=json.dumps(base_data),
            workflow_type="workflow",
        )
        def_manager.create(
            name="middle",
            definition_json=json.dumps(middle_data),
            workflow_type="workflow",
        )
        def_manager.create(
            name="top",
            definition_json=json.dumps(top_data),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("top")

        assert wf is not None
        assert wf.name == "top"
        step_names = [s.name for s in wf.steps]
        assert "base_step" in step_names
        assert "middle_step" in step_names
        assert "top_step" in step_names

    @pytest.mark.asyncio
    async def test_child_overrides_parent_step(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that a child step with the same name overrides the parent step."""
        parent_data = {
            "name": "parent",
            "version": "1.0.0",
            "steps": [
                {"name": "shared_step", "allowed_tools": "all", "description": "parent desc"},
            ],
        }
        child_data = {
            "name": "child",
            "version": "2.0",
            "extends": "parent",
            "steps": [
                {"name": "shared_step", "allowed_tools": ["read"], "description": "child desc"},
            ],
        }
        def_manager.create(
            name="parent",
            definition_json=json.dumps(parent_data),
            workflow_type="workflow",
        )
        def_manager.create(
            name="child",
            definition_json=json.dumps(child_data),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("child")

        assert wf is not None
        shared = next(s for s in wf.steps if s.name == "shared_step")
        assert shared.description == "child desc"
        assert shared.allowed_tools == ["read"]

    @pytest.mark.asyncio
    async def test_child_inherits_parent_description(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that child inherits top-level fields from parent."""
        parent_data = {
            "name": "parent",
            "version": "1.0.0",
            "description": "Parent description",
            "steps": [{"name": "step1", "allowed_tools": "all"}],
        }
        child_data = {
            "name": "child",
            "version": "2.0",
            "extends": "parent",
            "steps": [{"name": "step2", "allowed_tools": "all"}],
        }
        def_manager.create(
            name="parent",
            definition_json=json.dumps(parent_data),
            workflow_type="workflow",
        )
        def_manager.create(
            name="child",
            definition_json=json.dumps(child_data),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("child")

        assert wf is not None
        assert wf.description == "Parent description"


# ---------------------------------------------------------------------------
# TestMergeWorkflows -- _merge_workflows helper (unchanged)
# ---------------------------------------------------------------------------


class TestMergeWorkflows:
    """Tests for _merge_workflows method."""

    def test_simple_merge(self, loader: WorkflowLoader) -> None:
        """Test basic parent/child merge."""
        parent: dict[str, Any] = {
            "name": "parent",
            "version": "1.0.0",
            "description": "Parent desc",
        }
        child: dict[str, Any] = {"name": "child", "version": "2.0"}

        result = loader._merge_workflows(parent, child)

        assert result["name"] == "child"
        assert result["version"] == "2.0"
        assert result["description"] == "Parent desc"

    def test_nested_dict_merge(self, loader: WorkflowLoader) -> None:
        """Test that nested dicts are deep merged."""
        parent: dict[str, Any] = {
            "name": "parent",
            "settings": {"timeout": 30, "retry": True},
        }
        child: dict[str, Any] = {
            "name": "child",
            "settings": {"timeout": 60},
        }

        result = loader._merge_workflows(parent, child)

        assert result["settings"]["timeout"] == 60
        assert result["settings"]["retry"] is True

    def test_steps_merge_by_name(self, loader: WorkflowLoader) -> None:
        """Test that steps/phases are merged by name."""
        parent: dict[str, Any] = {
            "name": "parent",
            "steps": [
                {"name": "step1", "allowed_tools": "all"},
                {"name": "step2", "allowed_tools": ["read"]},
            ],
        }
        child: dict[str, Any] = {
            "name": "child",
            "steps": [
                {"name": "step2", "allowed_tools": ["read", "write"]},
                {"name": "step3", "allowed_tools": ["exec"]},
            ],
        }

        result = loader._merge_workflows(parent, child)

        assert len(result["steps"]) == 3
        step_map = {s["name"]: s for s in result["steps"]}
        assert step_map["step1"]["allowed_tools"] == "all"
        assert step_map["step2"]["allowed_tools"] == ["read", "write"]
        assert step_map["step3"]["allowed_tools"] == ["exec"]

    def test_phases_merge_by_name(self, loader: WorkflowLoader) -> None:
        """Test that 'phases' key (legacy) is merged correctly."""
        parent: dict[str, Any] = {
            "name": "parent",
            "phases": [
                {"name": "phase1", "tools": ["tool1"]},
            ],
        }
        child: dict[str, Any] = {
            "name": "child",
            "phases": [
                {"name": "phase1", "tools": ["tool1", "tool2"]},
                {"name": "phase2", "tools": ["tool3"]},
            ],
        }

        result = loader._merge_workflows(parent, child)

        assert len(result["phases"]) == 2


# ---------------------------------------------------------------------------
# TestMergeSteps -- _merge_steps helper (unchanged)
# ---------------------------------------------------------------------------


class TestMergeSteps:
    """Tests for _merge_steps method."""

    def test_merge_steps_update_existing(self, loader: WorkflowLoader) -> None:
        """Test that existing steps are updated."""
        parent_steps: list[dict[str, Any]] = [
            {"name": "step1", "timeout": 30},
            {"name": "step2", "timeout": 60},
        ]
        child_steps: list[dict[str, Any]] = [
            {"name": "step1", "timeout": 120},
        ]

        result = loader._merge_steps(parent_steps, child_steps)

        step_map = {s["name"]: s for s in result}
        assert step_map["step1"]["timeout"] == 120
        assert step_map["step2"]["timeout"] == 60

    def test_merge_steps_add_new(self, loader: WorkflowLoader) -> None:
        """Test that new steps are added."""
        parent_steps: list[dict[str, Any]] = [
            {"name": "step1", "timeout": 30},
        ]
        child_steps: list[dict[str, Any]] = [
            {"name": "step2", "timeout": 60},
        ]

        result = loader._merge_steps(parent_steps, child_steps)

        assert len(result) == 2
        step_names = [s["name"] for s in result]
        assert "step1" in step_names
        assert "step2" in step_names

    def test_merge_steps_without_name_parent(self, loader: WorkflowLoader) -> None:
        """Test that parent steps without 'name' key are skipped with warning."""
        parent_steps: list[dict[str, Any]] = [
            {"timeout": 30},
            {"name": "step1", "timeout": 60},
        ]
        child_steps: list[dict[str, Any]] = [
            {"name": "step2", "timeout": 90},
        ]

        result = loader._merge_steps(parent_steps, child_steps)

        step_names = [s["name"] for s in result]
        assert "step1" in step_names
        assert "step2" in step_names
        assert len(result) == 2

    def test_merge_steps_without_name_child(self, loader: WorkflowLoader) -> None:
        """Test that child steps without 'name' key are skipped with warning."""
        parent_steps: list[dict[str, Any]] = [
            {"name": "step1", "timeout": 30},
        ]
        child_steps: list[dict[str, Any]] = [
            {"timeout": 60},
            {"name": "step2", "timeout": 90},
        ]

        result = loader._merge_steps(parent_steps, child_steps)

        step_names = [s["name"] for s in result]
        assert "step1" in step_names
        assert "step2" in step_names
        assert len(result) == 2


# ---------------------------------------------------------------------------
# TestDiscoverLifecycleWorkflows -- DB-backed discovery
# ---------------------------------------------------------------------------


class TestDiscoverLifecycleWorkflows:
    """Tests for discover_workflows method (formerly discover_lifecycle_workflows)."""

    @pytest.mark.asyncio
    async def test_discover_from_db(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test discovering workflows from the database."""
        definition_data = {
            "name": "session_start",
            "version": "1.0.0",
            "type": "lifecycle",
        }
        def_manager.create(
            name="session_start",
            definition_json=json.dumps(definition_data),
            workflow_type="workflow",
            priority=10,
        )

        discovered = await loader.discover_workflows()

        assert len(discovered) == 1
        assert discovered[0].name == "session_start"
        assert discovered[0].is_project is False
        assert discovered[0].priority == 10

    @pytest.mark.asyncio
    async def test_discover_project_shadows_global(
        self,
        db: HubDatabase,
        project: Project,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that project workflows shadow global ones with the same name."""
        global_data = {
            "name": "session_start",
            "version": "1.0.0",
            "type": "lifecycle",
        }
        project_data = {
            "name": "session_start",
            "version": "2.0",
            "type": "lifecycle",
        }
        def_manager.create(
            name="session_start",
            definition_json=json.dumps(global_data),
            workflow_type="workflow",
            priority=100,
        )
        def_manager.create(
            name="session_start",
            definition_json=json.dumps(project_data),
            workflow_type="workflow",
            priority=50,
            project_id=project.id,
        )

        loader = WorkflowLoader(db=db)
        discovered = await loader.discover_workflows(project_path=project.id)

        # Project entry should shadow the global one
        session_entries = [d for d in discovered if d.name == "session_start"]
        assert len(session_entries) == 1
        assert session_entries[0].is_project is True
        assert session_entries[0].priority == 50

    @pytest.mark.asyncio
    async def test_discover_sorting(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that workflows are sorted by priority (ascending), then name."""
        for name, priority in [("b_workflow", 50), ("a_workflow", 100), ("c_workflow", 50)]:
            data = {"name": name, "version": "1.0.0", "type": "lifecycle"}
            def_manager.create(
                name=name,
                definition_json=json.dumps(data),
                workflow_type="workflow",
                priority=priority,
            )

        discovered = await loader.discover_workflows()

        names = [w.name for w in discovered]
        assert names == ["b_workflow", "c_workflow", "a_workflow"]

    @pytest.mark.asyncio
    async def test_discover_returns_all_workflow_types(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that discover_workflows returns all types (lifecycle + step)."""
        for name, wf_type in [("lifecycle_wf", "lifecycle"), ("step_wf", "step")]:
            data = {"name": name, "version": "1.0.0", "type": wf_type}
            def_manager.create(
                name=name,
                definition_json=json.dumps(data),
                workflow_type="workflow",
            )

        discovered = await loader.discover_workflows()

        assert len(discovered) == 2
        names = [w.name for w in discovered]
        assert "lifecycle_wf" in names
        assert "step_wf" in names

    @pytest.mark.asyncio
    async def test_discover_caching(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that discovery results are cached."""
        data = {"name": "cached_workflow", "version": "1.0.0", "type": "lifecycle"}
        def_manager.create(
            name="cached_workflow",
            definition_json=json.dumps(data),
            workflow_type="workflow",
        )

        discovered1 = await loader.discover_workflows()
        discovered2 = await loader.discover_workflows()

        assert discovered1 is discovered2

    @pytest.mark.asyncio
    async def test_discover_default_priority(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that workflows without explicit priority get the default of 100."""
        data = {"name": "no_priority", "version": "1.0.0", "type": "lifecycle"}
        def_manager.create(
            name="no_priority",
            definition_json=json.dumps(data),
            workflow_type="workflow",
            # priority defaults to 100 in def_manager.create()
        )

        discovered = await loader.discover_workflows()

        assert len(discovered) == 1
        assert discovered[0].priority == 100


# ---------------------------------------------------------------------------
# TestClearCache -- DB-backed
# ---------------------------------------------------------------------------


class TestClearCache:
    """Tests for clear_cache method."""

    @pytest.mark.asyncio
    async def test_clear_cache(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that both definition and discovery caches are cleared."""
        data = {"name": "test_workflow", "version": "1.0.0", "type": "lifecycle"}
        def_manager.create(
            name="test_workflow",
            definition_json=json.dumps(data),
            workflow_type="workflow",
        )

        # Populate caches
        await loader.load_workflow("test_workflow")
        await loader.discover_workflows()
        assert len(loader._cache) > 0
        assert len(loader._discovery_cache) > 0

        # Clear
        loader.clear_cache()

        assert len(loader._cache) == 0
        assert len(loader._discovery_cache) == 0


# ---------------------------------------------------------------------------
# TestValidateWorkflowForAgent -- uses mocks on load_workflow (unchanged)
# ---------------------------------------------------------------------------


class TestValidateWorkflowForAgent:
    """Tests for validate_workflow_for_agent method."""

    @pytest.mark.asyncio
    async def test_validate_nonexistent_workflow(self, loader: WorkflowLoader) -> None:
        """Test that nonexistent workflows are considered valid (no error)."""
        with patch.object(loader, "load_workflow", return_value=None):
            is_valid, error = await loader.validate_workflow_for_agent("nonexistent")

        assert is_valid is True
        assert error is None

    @pytest.mark.asyncio
    async def test_validate_step_workflow(self, loader: WorkflowLoader) -> None:
        """Test that on-demand (enabled=False) workflows are valid for agents."""
        step_workflow = MagicMock(spec=WorkflowDefinition)
        step_workflow.enabled = False

        with patch.object(loader, "load_workflow", return_value=step_workflow):
            is_valid, error = await loader.validate_workflow_for_agent("step_wf")

        assert is_valid is True
        assert error is None

    @pytest.mark.asyncio
    async def test_validate_lifecycle_workflow(self, loader: WorkflowLoader) -> None:
        """Test that always-on (enabled=True) workflows are invalid for agents."""
        lifecycle_workflow = MagicMock(spec=WorkflowDefinition)
        lifecycle_workflow.enabled = True

        with patch.object(loader, "load_workflow", return_value=lifecycle_workflow):
            is_valid, error = await loader.validate_workflow_for_agent("lifecycle_wf")

        assert is_valid is False
        assert "always-on" in error.lower()
        assert "on-demand" in error.lower()

    @pytest.mark.asyncio
    async def test_validate_with_loading_error(self, loader: WorkflowLoader) -> None:
        """Test handling of ValueError during workflow loading."""
        with patch.object(
            loader,
            "load_workflow",
            side_effect=ValueError("Circular inheritance"),
        ):
            is_valid, error = await loader.validate_workflow_for_agent("broken_wf")

        assert is_valid is False
        assert "Failed to load" in error
        assert "Circular inheritance" in error

    @pytest.mark.asyncio
    async def test_validate_with_project_path(self, loader: WorkflowLoader) -> None:
        """Test that project_path is passed through to load_workflow."""
        step_workflow = MagicMock(spec=WorkflowDefinition)
        step_workflow.type = "step"
        step_workflow.enabled = False

        with patch.object(loader, "load_workflow", return_value=step_workflow) as mock_load:
            await loader.validate_workflow_for_agent("test_wf", project_path="/my/project")

        mock_load.assert_called_once_with("test_wf", project_path="/my/project")
        assert mock_load.call_count == 1
        assert mock_load.call_args is not None


# ---------------------------------------------------------------------------
# TestDiscoveredWorkflow -- dataclass (unchanged)
# ---------------------------------------------------------------------------


class TestDiscoveredWorkflow:
    """Tests for DiscoveredWorkflow dataclass."""

    def test_dataclass_creation(self) -> None:
        """Test creating a DiscoveredWorkflow instance."""
        definition = MagicMock(spec=WorkflowDefinition)
        definition.type = "lifecycle"

        discovered = DiscoveredWorkflow(
            name="test",
            definition=definition,
            priority=50,
            is_project=True,
            path=Path("/test/path.yaml"),
        )

        assert discovered.name == "test"
        assert discovered.priority == 50
        assert discovered.is_project is True
        assert discovered.path == Path("/test/path.yaml")


# ---------------------------------------------------------------------------
# TestGenericWorkflow -- generic workflow loaded from DB
# ---------------------------------------------------------------------------


class TestGenericWorkflow:
    """Tests for a generic workflow definition loaded from the database."""

    @staticmethod
    def _generic_definition() -> dict[str, Any]:
        """Return a generic workflow definition dict for testing."""
        return {
            "name": "generic",
            "description": "Default workflow for generic agents",
            "version": "1.0",
            "type": "step",
            "steps": [
                {
                    "name": "work",
                    "description": "Work on the assigned task",
                    "allowed_tools": [
                        "Read",
                        "Write",
                        "Edit",
                        "Bash",
                        "Glob",
                        "Grep",
                        "WebFetch",
                        "WebSearch",
                        "NotebookEdit",
                        "mcp__gobby__call_tool",
                        "mcp__gobby__list_tools",
                        "get_task",
                        "update_task",
                        "close_task",
                        "list_tasks",
                        "remember",
                        "recall",
                    ],
                    "blocked_tools": ["spawn_agent"],
                    "transitions": [
                        {"to": "complete", "when": "task_completed or user_exit"},
                    ],
                },
                {
                    "name": "complete",
                    "description": "Work complete",
                },
            ],
            "exit_condition": "current_step == 'complete'",
        }

    @pytest.mark.asyncio
    async def test_generic_workflow_loads_successfully(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that the generic workflow can be loaded from DB."""
        def_manager.create(
            name="generic",
            definition_json=json.dumps(self._generic_definition()),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("generic")

        assert wf is not None
        assert wf.name == "generic"
        assert wf.type == "step"

    @pytest.mark.asyncio
    async def test_generic_workflow_has_work_and_complete_steps(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that the generic workflow has work and complete steps."""
        def_manager.create(
            name="generic",
            definition_json=json.dumps(self._generic_definition()),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("generic")

        assert wf is not None
        step_names = [s.name for s in wf.steps]
        assert "work" in step_names
        assert "complete" in step_names

    @pytest.mark.asyncio
    async def test_generic_workflow_work_step_has_allowed_tools(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that the work step allows basic file tools."""
        def_manager.create(
            name="generic",
            definition_json=json.dumps(self._generic_definition()),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("generic")
        work_step = next(s for s in wf.steps if s.name == "work")

        assert "Read" in work_step.allowed_tools
        assert "Write" in work_step.allowed_tools
        assert "Edit" in work_step.allowed_tools
        assert "Bash" in work_step.allowed_tools
        assert "Glob" in work_step.allowed_tools
        assert "Grep" in work_step.allowed_tools

    @pytest.mark.asyncio
    async def test_generic_workflow_blocks_spawn_tools(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that the work step blocks spawn tools to prevent recursive spawning."""
        def_manager.create(
            name="generic",
            definition_json=json.dumps(self._generic_definition()),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("generic")
        work_step = next(s for s in wf.steps if s.name == "work")

        assert "spawn_agent" in work_step.blocked_tools


# ---------------------------------------------------------------------------
# TestDiscoverWorkflows -- unified discover_workflows (DB-backed)
# ---------------------------------------------------------------------------


class TestDiscoverWorkflows:
    """Tests for the unified discover_workflows method."""

    @pytest.mark.asyncio
    async def test_discover_multiple_workflows(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test discovering multiple workflows from the database."""
        for name, priority in [("auto-task", 25), ("session-lifecycle", 10)]:
            data = {"name": name, "version": "1.0"}
            def_manager.create(
                name=name,
                definition_json=json.dumps(data),
                workflow_type="workflow",
                priority=priority,
            )

        discovered = await loader.discover_workflows()

        names = [w.name for w in discovered]
        assert "auto-task" in names
        assert "session-lifecycle" in names

    @pytest.mark.asyncio
    async def test_discover_sorted_by_priority(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that workflows are sorted by priority (lower first), then alphabetically."""
        for name, priority in [("high", 100), ("low", 10), ("mid", 50)]:
            data = {"name": name, "version": "1.0"}
            def_manager.create(
                name=name,
                definition_json=json.dumps(data),
                workflow_type="workflow",
                priority=priority,
            )

        discovered = await loader.discover_workflows()

        names = [w.name for w in discovered]
        assert names == ["low", "mid", "high"]

    @pytest.mark.asyncio
    async def test_discover_source_filtering_preserved(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that source filtering field is preserved on loaded definitions."""
        data: dict[str, Any] = {"name": "claude-only", "version": "1.0", "sources": ["claude"]}
        def_manager.create(
            name="claude-only",
            definition_json=json.dumps(data),
            workflow_type="workflow",
        )

        discovered = await loader.discover_workflows()

        assert len(discovered) == 1
        assert discovered[0].definition.sources == ["claude"]

    @pytest.mark.asyncio
    async def test_discover_workflows_caching_consistency(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that discover_workflows() returns consistent results across calls."""
        for name, priority in [("test", 10), ("root-wf", 20)]:
            data = {"name": name, "version": "1.0"}
            def_manager.create(
                name=name,
                definition_json=json.dumps(data),
                workflow_type="workflow",
                priority=priority,
            )

        first = await loader.discover_workflows()
        # Clear discovery cache so second call runs fresh
        loader._discovery_cache.clear()

        second = await loader.discover_workflows()

        assert [w.name for w in first] == [w.name for w in second]

    @pytest.mark.asyncio
    async def test_discover_project_shadows_global(
        self,
        db: HubDatabase,
        project: Project,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that project workflows shadow global ones with the same name."""
        def_manager.create(
            name="shared",
            definition_json=json.dumps({"name": "shared", "version": "1.0"}),
            workflow_type="workflow",
            priority=100,
        )
        def_manager.create(
            name="shared",
            definition_json=json.dumps({"name": "shared", "version": "2.0"}),
            workflow_type="workflow",
            priority=50,
            project_id=project.id,
        )

        loader = WorkflowLoader(db=db)
        discovered = await loader.discover_workflows(project_path=project.id)

        shared_entries = [d for d in discovered if d.name == "shared"]
        assert len(shared_entries) == 1
        assert shared_entries[0].is_project is True
        assert shared_entries[0].priority == 50

    @pytest.mark.asyncio
    async def test_discover_derives_enabled_from_type(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that when definition has 'type' but no 'enabled', enabled is derived from type."""
        def_manager.create(
            name="lifecycle-style",
            definition_json=json.dumps(
                {"name": "lifecycle-style", "version": "1.0", "type": "lifecycle"}
            ),
            workflow_type="workflow",
        )
        def_manager.create(
            name="step-style",
            definition_json=json.dumps({"name": "step-style", "version": "1.0", "type": "step"}),
            workflow_type="workflow",
        )

        discovered = await loader.discover_workflows()

        by_name = {w.name: w for w in discovered}
        assert by_name["lifecycle-style"].definition.enabled is True
        assert by_name["step-style"].definition.enabled is False

    @pytest.mark.asyncio
    async def test_discover_uses_row_priority(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that priority comes from the DB row's priority field."""
        def_manager.create(
            name="new-style",
            definition_json=json.dumps({"name": "new-style", "version": "1.0"}),
            workflow_type="workflow",
            priority=25,
        )

        discovered = await loader.discover_workflows()

        assert len(discovered) == 1
        assert discovered[0].priority == 25


# ---------------------------------------------------------------------------
# TestDBFirstLookup -- DB-only lookup (updated from DB-first)
# ---------------------------------------------------------------------------


class TestDBFirstLookup:
    """Tests for WorkflowLoader DB-only lookup behaviour."""

    @pytest.mark.asyncio
    async def test_load_workflow_from_db(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that load_workflow returns a definition from DB when found."""
        definition_data = {
            "name": "db-workflow",
            "version": "1.0",
            "steps": [{"name": "work", "allowed_tools": "all"}],
        }
        def_manager.create(
            name="db-workflow",
            definition_json=json.dumps(definition_data),
            workflow_type="workflow",
        )

        result = await loader.load_workflow("db-workflow")

        assert result is not None
        assert result.name == "db-workflow"
        assert isinstance(result, WorkflowDefinition)

    @pytest.mark.asyncio
    async def test_load_pipeline_from_db(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that load_workflow returns a PipelineDefinition from DB when type=pipeline."""
        definition_data = {
            "name": "db-pipeline",
            "type": "pipeline",
            "steps": [{"id": "build", "exec": "make build"}],
        }
        def_manager.create(
            name="db-pipeline",
            definition_json=json.dumps(definition_data),
            workflow_type="pipeline",
        )

        result = await loader.load_workflow("db-pipeline")

        assert result is not None
        assert isinstance(result, PipelineDefinition)
        assert result.name == "db-pipeline"

    @pytest.mark.asyncio
    async def test_load_workflow_db_only(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that DB definition is the sole source of truth."""
        definition_data = {
            "name": "shadowed",
            "description": "from-db",
            "version": "2.0",
            "steps": [{"name": "work", "allowed_tools": "all"}],
        }
        def_manager.create(
            name="shadowed",
            definition_json=json.dumps(definition_data),
            workflow_type="workflow",
        )

        result = await loader.load_workflow("shadowed")

        assert result is not None
        assert result.description == "from-db"
        assert result.version == "2.0"

    @pytest.mark.asyncio
    async def test_load_workflow_returns_none_when_not_in_db(
        self,
        loader: WorkflowLoader,
    ) -> None:
        """Test that load_workflow returns None when no DB entry exists (no filesystem fallback)."""
        result = await loader.load_workflow("fs-only")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_workflow_db_disabled_still_returned(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that disabled DB definitions are still returned by load_workflow.

        Enabled/disabled filtering is done at the engine level, not the loader level.
        """
        definition_data = {
            "name": "disabled-wf",
            "steps": [{"name": "work", "allowed_tools": "all"}],
        }
        def_manager.create(
            name="disabled-wf",
            definition_json=json.dumps(definition_data),
            workflow_type="workflow",
            enabled=False,
        )

        result = await loader.load_workflow("disabled-wf")

        assert result is not None
        assert result.name == "disabled-wf"

    @pytest.mark.asyncio
    async def test_load_workflow_db_caches_result(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that DB-loaded workflow is cached for subsequent calls."""
        definition_data = {
            "name": "cached-wf",
            "steps": [{"name": "work", "allowed_tools": "all"}],
        }
        def_manager.create(
            name="cached-wf",
            definition_json=json.dumps(definition_data),
            workflow_type="workflow",
        )

        result1 = await loader.load_workflow("cached-wf")
        result2 = await loader.load_workflow("cached-wf")

        assert result1 is not None
        assert result2 is not None
        assert result1 is result2

    @pytest.mark.asyncio
    async def test_discover_workflows_includes_db_entries(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that discover_workflows includes DB workflow definitions."""
        definition_data = {
            "name": "db-discovered",
            "steps": [{"name": "work", "allowed_tools": "all"}],
        }
        def_manager.create(
            name="db-discovered",
            definition_json=json.dumps(definition_data),
            workflow_type="workflow",
        )

        discovered = await loader.discover_workflows()

        names = {d.name for d in discovered}
        assert "db-discovered" in names

    @pytest.mark.asyncio
    async def test_discover_workflows_db_shadows_by_name(
        self,
        db: HubDatabase,
        project: Project,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that DB entries shadow each other by name (project over global)."""
        def_manager.create(
            name="shadow-test",
            definition_json=json.dumps(
                {
                    "name": "shadow-test",
                    "description": "global",
                    "steps": [{"name": "work", "allowed_tools": "all"}],
                }
            ),
            workflow_type="workflow",
            priority=100,
        )
        def_manager.create(
            name="shadow-test",
            definition_json=json.dumps(
                {
                    "name": "shadow-test",
                    "description": "project",
                    "steps": [{"name": "work", "allowed_tools": "all"}],
                }
            ),
            workflow_type="workflow",
            priority=10,
            project_id=project.id,
        )

        loader = WorkflowLoader(db=db)
        discovered = await loader.discover_workflows(project_path=project.id)

        shadow_entries = [d for d in discovered if d.name == "shadow-test"]
        assert len(shadow_entries) == 1
        assert shadow_entries[0].definition.description == "project"
        assert shadow_entries[0].priority == 10

    @pytest.mark.asyncio
    async def test_discover_workflows_db_only(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that discover only returns DB entries (no filesystem)."""
        def_manager.create(
            name="db-only",
            definition_json=json.dumps(
                {"name": "db-only", "steps": [{"name": "work", "allowed_tools": "all"}]}
            ),
            workflow_type="workflow",
        )

        discovered = await loader.discover_workflows()
        names = {d.name for d in discovered}

        assert "db-only" in names
        # No filesystem-only entries should appear
        assert len(names) == 1
