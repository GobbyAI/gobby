"""
Tests for workflow variable loading and merging.

Tests the following scenarios:
1. WorkflowDefinition.variables loaded from DB
2. Variable inheritance when workflows extend each other
"""

import json

import pytest

from gobby.workflows.loader import WorkflowLoader

pytestmark = pytest.mark.unit


# =============================================================================
# DB fixtures
# =============================================================================


@pytest.fixture
def db(tmp_path):
    """Create a fresh database with migrations applied."""
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations

    db_path = tmp_path / "test_wf_vars.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def def_manager(db):
    """Create a workflow definition manager."""
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

    return LocalWorkflowDefinitionManager(db)


@pytest.fixture
def loader(db):
    """Create a WorkflowLoader backed by the temp database."""
    return WorkflowLoader(db=db)


# =============================================================================
# Test WorkflowDefinition Variables Loading from DB
# =============================================================================


class TestWorkflowDefinitionVariables:
    """Tests for loading variables from workflow definitions in DB."""

    @pytest.mark.asyncio
    async def test_load_workflow_with_variables(self, loader, def_manager) -> None:
        """Variables section is loaded into WorkflowDefinition."""
        def_manager.create(
            name="test_workflow",
            definition_json=json.dumps(
                {
                    "name": "test_workflow",
                    "version": "1.0.0",
                    "type": "lifecycle",
                    "variables": {
                        "require_task_before_edit": True,
                        "tdd_mode": False,
                        "session_task": None,
                    },
                    "steps": [],
                }
            ),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("test_workflow")

        assert wf is not None
        assert wf.variables == {
            "require_task_before_edit": True,
            "tdd_mode": False,
            "session_task": None,
        }

    @pytest.mark.asyncio
    async def test_load_workflow_without_variables(self, loader, def_manager) -> None:
        """Workflow without variables section has empty dict."""
        def_manager.create(
            name="no_vars_workflow",
            definition_json=json.dumps(
                {
                    "name": "no_vars_workflow",
                    "version": "1.0.0",
                    "steps": [],
                }
            ),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("no_vars_workflow")

        assert wf is not None
        assert wf.variables == {}

    @pytest.mark.asyncio
    async def test_variables_support_all_types(self, loader, def_manager) -> None:
        """Variables support string, int, float, bool, null, list, and dict values."""
        def_manager.create(
            name="typed_vars",
            definition_json=json.dumps(
                {
                    "name": "typed_vars",
                    "version": "1.0.0",
                    "variables": {
                        "string_var": "hello",
                        "int_var": 42,
                        "float_var": 3.14,
                        "bool_var": True,
                        "null_var": None,
                        "list_var": [1, 2, 3],
                        "dict_var": {"nested": "value"},
                    },
                    "steps": [],
                }
            ),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("typed_vars")

        assert wf.variables["string_var"] == "hello"
        assert wf.variables["int_var"] == 42
        assert wf.variables["float_var"] == 3.14
        assert wf.variables["bool_var"] is True
        assert wf.variables["null_var"] is None
        assert wf.variables["list_var"] == [1, 2, 3]
        assert wf.variables["dict_var"] == {"nested": "value"}


class TestWorkflowVariableInheritance:
    """Tests for variable inheritance when workflows extend each other."""

    @pytest.mark.asyncio
    async def test_child_inherits_parent_variables(self, loader, def_manager) -> None:
        """Child workflow inherits variables from parent."""
        def_manager.create(
            name="parent",
            definition_json=json.dumps(
                {
                    "name": "parent",
                    "version": "1.0.0",
                    "variables": {
                        "from_parent": "inherited",
                        "shared": "parent_value",
                    },
                    "steps": [],
                }
            ),
            workflow_type="workflow",
        )
        def_manager.create(
            name="child",
            definition_json=json.dumps(
                {
                    "name": "child",
                    "version": "1.0.0",
                    "extends": "parent",
                    "variables": {
                        "from_child": "new",
                    },
                    "steps": [],
                }
            ),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("child")

        assert wf is not None
        # Should have both parent and child variables
        assert wf.variables["from_parent"] == "inherited"
        assert wf.variables["from_child"] == "new"

    @pytest.mark.asyncio
    async def test_child_overrides_parent_variables(self, loader, def_manager) -> None:
        """Child variables override parent variables with same name."""
        def_manager.create(
            name="parent",
            definition_json=json.dumps(
                {
                    "name": "parent",
                    "version": "1.0.0",
                    "variables": {
                        "shared_var": "parent_value",
                        "only_parent": 100,
                    },
                    "steps": [],
                }
            ),
            workflow_type="workflow",
        )
        def_manager.create(
            name="child",
            definition_json=json.dumps(
                {
                    "name": "child",
                    "version": "1.0.0",
                    "extends": "parent",
                    "variables": {
                        "shared_var": "child_value",
                    },
                    "steps": [],
                }
            ),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("child")

        # Child overrides shared_var but inherits only_parent
        assert wf.variables["shared_var"] == "child_value"
        assert wf.variables["only_parent"] == 100

    @pytest.mark.asyncio
    async def test_three_level_inheritance_merges_variables(self, loader, def_manager) -> None:
        """Variables merge across three levels of inheritance."""
        def_manager.create(
            name="base",
            definition_json=json.dumps(
                {
                    "name": "base",
                    "version": "1.0.0",
                    "variables": {"level": "base", "from_base": True},
                    "steps": [],
                }
            ),
            workflow_type="workflow",
        )
        def_manager.create(
            name="middle",
            definition_json=json.dumps(
                {
                    "name": "middle",
                    "version": "1.0.0",
                    "extends": "base",
                    "variables": {"level": "middle", "from_middle": True},
                    "steps": [],
                }
            ),
            workflow_type="workflow",
        )
        def_manager.create(
            name="top",
            definition_json=json.dumps(
                {
                    "name": "top",
                    "version": "1.0.0",
                    "extends": "middle",
                    "variables": {"level": "top", "from_top": True},
                    "steps": [],
                }
            ),
            workflow_type="workflow",
        )

        wf = await loader.load_workflow("top")

        # Top overrides level, but inherits from all ancestors
        assert wf.variables["level"] == "top"
        assert wf.variables["from_base"] is True
        assert wf.variables["from_middle"] is True
        assert wf.variables["from_top"] is True
