"""Tests for the new skill-based task expansion MCP tools.

Tests cover:
- save_expansion_spec: Save expansion spec to task.expansion_context
- execute_expansion: Create subtasks atomically from saved spec
- get_expansion_spec: Check for pending expansion spec (resume after compaction)
"""

import json
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._expansion import create_expansion_registry
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db):
    """Create a task manager with the shared temp database."""
    return LocalTaskManager(temp_db)


@pytest.fixture
def sync_manager(task_manager, temp_dir):
    """Create a sync manager with task manager and temp directory."""
    return TaskSyncManager(task_manager, temp_dir / "tasks.jsonl")


@pytest.fixture
def test_project(project_manager):
    """Create a test project for expansion tests."""
    project = project_manager.create(
        name="test-project",
        repo_path="/tmp/test-project",
    )
    return project.id


@pytest.fixture
def test_session(session_manager, test_project):
    """Create a test session for expansion tests."""
    session = session_manager.register(
        project_id=test_project,
        source="test",
        external_id="test-external",
        machine_id="test-machine",
    )
    return session.id


@pytest.fixture
def expansion_registry(
    task_manager: LocalTaskManager,
    sync_manager: TaskSyncManager,
) -> dict:
    """Create expansion registry and return tools as dict."""
    ctx = RegistryContext(
        task_manager=task_manager,
        sync_manager=sync_manager,
        task_validator=None,
        agent_runner=None,
        config=None,
    )
    registry = create_expansion_registry(ctx)
    return registry._tools


@pytest.fixture
def parent_task(task_manager: LocalTaskManager, test_project: str) -> str:
    """Create a parent task for expansion tests."""
    task = task_manager.create_task(
        project_id=test_project,
        title="Parent task for expansion",
        task_type="feature",
    )
    return task.id


class TestSaveExpansionSpec:
    """Tests for save_expansion_spec tool."""

    @pytest.mark.asyncio
    async def test_save_valid_spec(
        self,
        expansion_registry: dict,
        parent_task: str,
        task_manager: LocalTaskManager,
    ) -> None:
        """Test saving a valid expansion spec."""
        save_fn = expansion_registry["save_expansion_spec"].func

        spec = {
            "subtasks": [
                {"title": "First subtask", "category": "code"},
                {"title": "Second subtask", "category": "test", "depends_on": [0]},
            ]
        }

        result = await save_fn(task_id=parent_task, spec=spec)

        assert result["saved"] is True
        assert result["subtask_count"] == 2

        # Verify task was updated
        task = task_manager.get_task(parent_task)
        assert task.expansion_status == "pending"
        assert task.expansion_context is not None
        saved_spec = json.loads(task.expansion_context)
        assert len(saved_spec["subtasks"]) == 2

    @pytest.mark.asyncio
    async def test_save_spec_missing_subtasks(
        self,
        expansion_registry: dict,
        parent_task: str,
    ) -> None:
        """Test saving spec without subtasks array."""
        save_fn = expansion_registry["save_expansion_spec"].func

        spec = {"invalid": "spec"}

        result = await save_fn(task_id=parent_task, spec=spec)

        assert "error" in result
        assert "subtasks" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_save_spec_empty_subtasks(
        self,
        expansion_registry: dict,
        parent_task: str,
    ) -> None:
        """Test saving spec with empty subtasks array."""
        save_fn = expansion_registry["save_expansion_spec"].func

        spec = {"subtasks": []}

        result = await save_fn(task_id=parent_task, spec=spec)

        assert "error" in result
        assert "at least one" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_save_spec_subtask_missing_title(
        self,
        expansion_registry: dict,
        parent_task: str,
    ) -> None:
        """Test saving spec with subtask missing title."""
        save_fn = expansion_registry["save_expansion_spec"].func

        spec = {
            "subtasks": [
                {"category": "code"},  # Missing title
            ]
        }

        result = await save_fn(task_id=parent_task, spec=spec)

        assert "error" in result
        assert "title" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_save_spec_task_not_found(
        self,
        expansion_registry: dict,
    ) -> None:
        """Test saving spec to non-existent task."""
        save_fn = expansion_registry["save_expansion_spec"].func

        spec = {"subtasks": [{"title": "Test"}]}

        result = await save_fn(task_id="nonexistent-task-id", spec=spec)

        assert "error" in result
        assert "not found" in result["error"].lower()


class TestExecuteExpansion:
    """Tests for execute_expansion tool."""

    @pytest.mark.asyncio
    async def test_execute_pending_expansion(
        self,
        expansion_registry: dict,
        parent_task: str,
        task_manager: LocalTaskManager,
        test_project: str,
        test_session: str,
    ) -> None:
        """Test executing a pending expansion spec."""
        save_fn = expansion_registry["save_expansion_spec"].func
        execute_fn = expansion_registry["execute_expansion"].func

        # First save a spec
        spec = {
            "subtasks": [
                {"title": "Implement feature", "category": "code", "priority": 2},
                {"title": "Write tests", "category": "test", "depends_on": [0]},
            ]
        }
        await save_fn(task_id=parent_task, spec=spec)

        # Execute the expansion
        result = await execute_fn(parent_task_id=parent_task, session_id=test_session)

        assert "error" not in result
        assert result["count"] == 2
        assert len(result["created"]) == 2

        # Verify parent task updated
        task = task_manager.get_task(parent_task)
        assert task.is_expanded is True
        assert task.expansion_status == "completed"

        # Verify subtasks created
        subtasks = task_manager.list_tasks(
            project_id=test_project,
            parent_task_id=parent_task,
        )
        assert len(subtasks) == 2
        subtask_titles = {s.title for s in subtasks}
        assert subtask_titles == {"Implement feature", "Write tests"}

    @pytest.mark.asyncio
    async def test_execute_no_pending_expansion(
        self,
        expansion_registry: dict,
        parent_task: str,
        test_session: str,
    ) -> None:
        """Test executing when no pending expansion."""
        execute_fn = expansion_registry["execute_expansion"].func

        result = await execute_fn(parent_task_id=parent_task, session_id=test_session)

        assert "error" in result
        assert "no pending" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_execute_creates_dependencies(
        self,
        expansion_registry: dict,
        parent_task: str,
        task_manager: LocalTaskManager,
        test_project: str,
        test_session: str,
    ) -> None:
        """Test that execute_expansion creates proper dependencies."""
        save_fn = expansion_registry["save_expansion_spec"].func
        execute_fn = expansion_registry["execute_expansion"].func

        # Spec with dependencies
        spec = {
            "subtasks": [
                {"title": "First task"},
                {"title": "Second task", "depends_on": [0]},
                {"title": "Third task", "depends_on": [0, 1]},
            ]
        }
        await save_fn(task_id=parent_task, spec=spec)

        result = await execute_fn(parent_task_id=parent_task, session_id=test_session)

        assert result["count"] == 3

        # Get created subtasks
        subtasks = task_manager.list_tasks(
            project_id=test_project,
            parent_task_id=parent_task,
        )
        assert len(subtasks) == 3

        # Verify task refs are returned (must be #N format)
        for ref in result["created"]:
            assert ref.startswith("#"), f"Expected #N format, got {ref}"

    @pytest.mark.asyncio
    async def test_execute_task_not_found(
        self,
        expansion_registry: dict,
        test_session: str,
    ) -> None:
        """Test executing expansion on non-existent task."""
        execute_fn = expansion_registry["execute_expansion"].func

        result = await execute_fn(parent_task_id="nonexistent", session_id=test_session)

        assert "error" in result
        assert "not found" in result["error"].lower()


class TestGetExpansionSpec:
    """Tests for get_expansion_spec tool."""

    @pytest.mark.asyncio
    async def test_get_pending_spec(
        self,
        expansion_registry: dict,
        parent_task: str,
    ) -> None:
        """Test getting a pending expansion spec."""
        save_fn = expansion_registry["save_expansion_spec"].func
        get_fn = expansion_registry["get_expansion_spec"].func

        # Save a spec
        spec = {
            "subtasks": [
                {"title": "Task 1"},
                {"title": "Task 2"},
            ]
        }
        await save_fn(task_id=parent_task, spec=spec)

        # Get the spec
        result = await get_fn(task_id=parent_task)

        assert result["pending"] is True
        assert result["subtask_count"] == 2
        assert "spec" in result
        assert len(result["spec"]["subtasks"]) == 2

    @pytest.mark.asyncio
    async def test_get_no_pending_spec(
        self,
        expansion_registry: dict,
        parent_task: str,
    ) -> None:
        """Test getting spec when none is pending."""
        get_fn = expansion_registry["get_expansion_spec"].func

        result = await get_fn(task_id=parent_task)

        assert result["pending"] is False

    @pytest.mark.asyncio
    async def test_get_spec_after_execution(
        self,
        expansion_registry: dict,
        parent_task: str,
        test_session: str,
    ) -> None:
        """Test that get_expansion_spec returns pending=False after execution."""
        save_fn = expansion_registry["save_expansion_spec"].func
        execute_fn = expansion_registry["execute_expansion"].func
        get_fn = expansion_registry["get_expansion_spec"].func

        # Save and execute
        spec = {"subtasks": [{"title": "Task 1"}]}
        await save_fn(task_id=parent_task, spec=spec)
        await execute_fn(parent_task_id=parent_task, session_id=test_session)

        # Get should now show not pending
        result = await get_fn(task_id=parent_task)

        assert result["pending"] is False

    @pytest.mark.asyncio
    async def test_get_spec_task_not_found(
        self,
        expansion_registry: dict,
    ) -> None:
        """Test getting spec for non-existent task."""
        get_fn = expansion_registry["get_expansion_spec"].func

        result = await get_fn(task_id="nonexistent")

        assert "error" in result
        assert "not found" in result["error"].lower()


class TestValidateExpansionSpec:
    """Tests for validate_expansion_spec tool."""

    @pytest.mark.asyncio
    async def test_validate_valid_spec(
        self,
        expansion_registry: dict,
        parent_task: str,
    ) -> None:
        """Test validating a structurally correct spec."""
        save_fn = expansion_registry["save_expansion_spec"].func
        validate_fn = expansion_registry["validate_expansion_spec"].func

        spec = {
            "subtasks": [
                {
                    "title": "First task",
                    "description": "Do the first thing",
                    "category": "code",
                },
                {
                    "title": "Second task",
                    "description": "Do the second thing",
                    "category": "code",
                    "depends_on": [0],
                },
            ]
        }
        await save_fn(task_id=parent_task, spec=spec)

        result = await validate_fn(task_id=parent_task)

        assert result["valid"] is True
        assert result["errors"] == []
        assert result["subtask_count"] == 2

    @pytest.mark.asyncio
    async def test_validate_no_spec(
        self,
        expansion_registry: dict,
        parent_task: str,
    ) -> None:
        """Test validating when no spec is saved."""
        validate_fn = expansion_registry["validate_expansion_spec"].func

        result = await validate_fn(task_id=parent_task)

        assert "error" in result
        assert "no expansion spec" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_validate_empty_subtasks(
        self,
        expansion_registry: dict,
        parent_task: str,
        task_manager: LocalTaskManager,
    ) -> None:
        """Test validating spec with empty subtasks list."""
        # Directly set expansion_context to bypass save_expansion_spec validation
        task_manager.update_task(
            parent_task,
            expansion_context=json.dumps({"subtasks": []}),
            expansion_status="pending",
        )
        validate_fn = expansion_registry["validate_expansion_spec"].func

        result = await validate_fn(task_id=parent_task)

        assert result["valid"] is False
        assert any("no subtasks" in e.lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_validate_missing_required_fields(
        self,
        expansion_registry: dict,
        parent_task: str,
        task_manager: LocalTaskManager,
    ) -> None:
        """Test validating spec with missing title, description, category."""
        task_manager.update_task(
            parent_task,
            expansion_context=json.dumps(
                {
                    "subtasks": [
                        {"title": ""},  # empty title, no description, no category
                    ]
                }
            ),
            expansion_status="pending",
        )
        validate_fn = expansion_registry["validate_expansion_spec"].func

        result = await validate_fn(task_id=parent_task)

        assert result["valid"] is False
        errors_text = " ".join(result["errors"])
        assert "title" in errors_text.lower()
        assert "description" in errors_text.lower()
        assert "category" in errors_text.lower()

    @pytest.mark.asyncio
    async def test_validate_self_reference(
        self,
        expansion_registry: dict,
        parent_task: str,
    ) -> None:
        """Test validating spec with self-referencing dependency."""
        save_fn = expansion_registry["save_expansion_spec"].func
        validate_fn = expansion_registry["validate_expansion_spec"].func

        spec = {
            "subtasks": [
                {
                    "title": "Task A",
                    "description": "Do A",
                    "category": "code",
                    "depends_on": [0],  # self-reference
                },
            ]
        }
        await save_fn(task_id=parent_task, spec=spec)

        result = await validate_fn(task_id=parent_task)

        assert result["valid"] is False
        assert any("self-reference" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_validate_out_of_bounds_dep(
        self,
        expansion_registry: dict,
        parent_task: str,
    ) -> None:
        """Test validating spec with out-of-bounds dependency index."""
        save_fn = expansion_registry["save_expansion_spec"].func
        validate_fn = expansion_registry["validate_expansion_spec"].func

        spec = {
            "subtasks": [
                {
                    "title": "Task A",
                    "description": "Do A",
                    "category": "code",
                    "depends_on": [5],  # out of bounds
                },
            ]
        }
        await save_fn(task_id=parent_task, spec=spec)

        result = await validate_fn(task_id=parent_task)

        assert result["valid"] is False
        assert any("out of bounds" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_validate_circular_dependency(
        self,
        expansion_registry: dict,
        parent_task: str,
    ) -> None:
        """Test validating spec with circular dependencies."""
        save_fn = expansion_registry["save_expansion_spec"].func
        validate_fn = expansion_registry["validate_expansion_spec"].func

        spec = {
            "subtasks": [
                {
                    "title": "Task A",
                    "description": "Do A",
                    "category": "code",
                    "depends_on": [1],
                },
                {
                    "title": "Task B",
                    "description": "Do B",
                    "category": "code",
                    "depends_on": [0],  # A->B->A cycle
                },
            ]
        }
        await save_fn(task_id=parent_task, spec=spec)

        result = await validate_fn(task_id=parent_task)

        assert result["valid"] is False
        assert any("circular" in e.lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_validate_plan_section_coverage_missing(
        self,
        expansion_registry: dict,
        task_manager: LocalTaskManager,
        test_project: str,
    ) -> None:
        """Test that validation catches missing plan section coverage."""
        # Create task with plan sections in description
        task = task_manager.create_task(
            project_id=test_project,
            title="Plan task",
            task_type="epic",
            description="## Plan\n\n### 1.1 Add models\nDetails...\n\n### 1.2 Add routes\nDetails...\n\n### 1.3 Add tests\nDetails...",
        )

        # Spec only covers 1.1 and 1.2, not 1.3
        spec = {
            "subtasks": [
                {
                    "title": "1.1 Add models",
                    "description": "Section 1.1 content",
                    "category": "code",
                },
                {
                    "title": "1.2 Add routes",
                    "description": "Section 1.2 content",
                    "category": "code",
                    "depends_on": [0],
                },
            ]
        }
        task_manager.update_task(
            task.id,
            expansion_context=json.dumps(spec),
            expansion_status="pending",
        )

        validate_fn = expansion_registry["validate_expansion_spec"].func
        result = await validate_fn(task_id=task.id)

        assert result["valid"] is False
        assert any("1.3" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_validate_plan_section_coverage_complete(
        self,
        expansion_registry: dict,
        task_manager: LocalTaskManager,
        test_project: str,
    ) -> None:
        """Test that validation passes when all plan sections are covered."""
        task = task_manager.create_task(
            project_id=test_project,
            title="Plan task",
            task_type="epic",
            description="### 1.1 Models\nContent\n\n### 1.2 Routes\nContent",
        )

        spec = {
            "subtasks": [
                {
                    "title": "1.1 Models",
                    "description": "Section 1.1 implementation",
                    "category": "code",
                },
                {
                    "title": "1.2 Routes",
                    "description": "Section 1.2 implementation",
                    "category": "code",
                },
            ]
        }
        task_manager.update_task(
            task.id,
            expansion_context=json.dumps(spec),
            expansion_status="pending",
        )

        validate_fn = expansion_registry["validate_expansion_spec"].func
        result = await validate_fn(task_id=task.id)

        assert result["valid"] is True
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_validate_task_not_found(
        self,
        expansion_registry: dict,
    ) -> None:
        """Test validating spec for non-existent task."""
        validate_fn = expansion_registry["validate_expansion_spec"].func

        result = await validate_fn(task_id="nonexistent")

        assert "error" in result
        assert "not found" in result["error"].lower()


class TestExpansionWithSeqNum:
    """Tests for expansion with sequential task numbers."""

    @pytest.mark.asyncio
    async def test_save_spec_with_seq_num_reference(
        self,
        expansion_registry: dict,
        task_manager: LocalTaskManager,
        test_project: str,
    ) -> None:
        """Test saving spec using #N reference."""
        save_fn = expansion_registry["save_expansion_spec"].func

        # Create task with seq_num
        task = task_manager.create_task(
            project_id=test_project,
            title="Task with seq num",
            task_type="feature",
        )

        # Mock project context so #N resolution works (patch both modules)
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.get_project_context",
                return_value={"id": test_project},
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
                return_value={"id": test_project},
            ),
        ):
            # Use #N reference
            spec = {"subtasks": [{"title": "Subtask"}]}
            save_result = await save_fn(task_id=f"#{task.seq_num}", spec=spec)

        assert save_result["saved"] is True
        assert save_result["task_id"] == task.id

    @pytest.mark.asyncio
    async def test_execute_expansion_returns_seq_refs(
        self,
        expansion_registry: dict,
        task_manager: LocalTaskManager,
        test_project: str,
        test_session: str,
    ) -> None:
        """Test that execute_expansion returns #N refs for created subtasks."""
        save_fn = expansion_registry["save_expansion_spec"].func
        execute_fn = expansion_registry["execute_expansion"].func

        # Create parent task
        task = task_manager.create_task(
            project_id=test_project,
            title="Parent for seq test",
            task_type="feature",
        )
        parent_id = task.id

        # Save and execute (use UUID directly for simplicity)
        spec = {
            "subtasks": [
                {"title": "Subtask 1"},
                {"title": "Subtask 2"},
            ]
        }
        await save_fn(task_id=parent_id, spec=spec)
        exec_result = await execute_fn(parent_task_id=parent_id, session_id=test_session)

        # All refs should be #N format
        for ref in exec_result["created"]:
            assert ref.startswith("#"), f"Expected #N format, got {ref}"
