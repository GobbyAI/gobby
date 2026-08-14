"""Tests for workflow/pipeline definition CRUD MCP tools."""

import json

import pytest

from gobby.mcp_proxy.tools.workflows._definitions import (
    create_workflow_definition,
    delete_workflow_definition,
    export_workflow_definition,
    update_workflow_definition,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.loader import WorkflowLoader

pytestmark = pytest.mark.unit

VALID_WORKFLOW_YAML = """\
name: test-workflow
description: A test workflow
version: "1.0"
type: pipeline
steps:
  - id: work
    exec: echo work
"""

VALID_PIPELINE_YAML = """\
name: test-pipeline
description: A test pipeline
type: pipeline
version: "1.0"
steps:
  - id: build
    exec: make build
"""

VALID_RULE_YAML = """\
name: test-rule
type: rule
event: before_tool
effects:
  - type: block
    reason: Test rule
"""

VALID_VARIABLE_YAML = """\
name: test-variable
type: variable
variable: test_value
value: 42
"""

VALID_AGENT_YAML = """\
name: test-agent
type: agent
role: Test agent
"""

INVALID_YAML_NO_NAME = """\
description: Missing name field
type: pipeline
steps:
  - id: work
    exec: echo work
"""

INVALID_YAML_BAD_PIPELINE = """\
name: bad-pipeline
type: pipeline
steps: []
"""


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a fresh database with migrations applied."""
    database = temp_db
    return database


@pytest.fixture
def def_manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


@pytest.fixture
def loader(tmp_path) -> WorkflowLoader:
    loader = WorkflowLoader()
    loader.global_dirs = [tmp_path / "workflows"]
    return loader


# =============================================================================
# create_workflow_definition
# =============================================================================


class TestCreateWorkflow:
    @pytest.mark.parametrize(
        ("yaml_content", "expected_type"),
        [
            (VALID_RULE_YAML, "rule"),
            (VALID_VARIABLE_YAML, "variable"),
        ],
    )
    def test_create_valid_non_pipeline_types(
        self,
        def_manager: LocalWorkflowDefinitionManager,
        loader: WorkflowLoader,
        yaml_content: str,
        expected_type: str,
    ) -> None:
        result = create_workflow_definition(def_manager, loader, yaml_content)

        assert result["success"] is True
        assert result["definition"]["workflow_type"] == expected_type

    def test_create_valid_workflow(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        loader.db = def_manager.db
        result = create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)

        assert result["success"] is True
        defn = result["definition"]
        assert defn["name"] == "test-workflow"
        assert defn["workflow_type"] == "pipeline"
        assert defn["description"] == "A test workflow"
        assert defn["version"] == "1.0"
        assert defn["enabled"] is True
        pipeline = loader.load_pipeline_sync("test-workflow")
        assert pipeline is not None
        assert pipeline.enabled is True

    def test_create_valid_pipeline(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        result = create_workflow_definition(def_manager, loader, VALID_PIPELINE_YAML)

        assert result["success"] is True
        defn = result["definition"]
        assert defn["name"] == "test-pipeline"
        assert defn["workflow_type"] == "pipeline"

    def test_create_pipeline_normalizes_disabled_string(
        self, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        loader = WorkflowLoader(def_manager.db)
        yaml_content = VALID_PIPELINE_YAML.replace(
            "type: pipeline", 'type: pipeline\nenabled: "false"'
        )

        result = create_workflow_definition(def_manager, loader, yaml_content)

        assert result["success"] is True
        assert result["definition"]["enabled"] is False
        pipeline = loader.load_pipeline_sync("test-pipeline")
        assert pipeline is not None
        assert pipeline.enabled is False

    def test_create_with_project_id(
        self,
        db: HubDatabase,
        def_manager: LocalWorkflowDefinitionManager,
        loader: WorkflowLoader,
    ) -> None:
        db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("11111111-1111-4111-8111-111111110001", "Test Project"),
        )

        result = create_workflow_definition(
            def_manager,
            loader,
            VALID_WORKFLOW_YAML,
            project_id="11111111-1111-4111-8111-111111110001",
        )

        assert result["success"] is True
        row = def_manager.get_by_name(
            "test-workflow", project_id="11111111-1111-4111-8111-111111110001"
        )
        assert row is not None
        assert row.project_id == "11111111-1111-4111-8111-111111110001"

    def test_create_rejects_invalid_yaml(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        result = create_workflow_definition(def_manager, loader, "not: [valid: yaml: {{")

        assert result["success"] is False
        assert "YAML parse error" in result["error"]

    def test_create_rejects_missing_name(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        result = create_workflow_definition(def_manager, loader, INVALID_YAML_NO_NAME)

        assert result["success"] is False
        assert "Validation failed" in result["error"]

    def test_create_rejects_pydantic_failures(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        result = create_workflow_definition(def_manager, loader, INVALID_YAML_BAD_PIPELINE)

        assert result["success"] is False
        assert "Validation failed" in result["error"]

    @pytest.mark.parametrize(
        "yaml_content",
        [
            VALID_RULE_YAML + "junk: true\n",
            VALID_VARIABLE_YAML + "junk: true\n",
            VALID_AGENT_YAML + "junk: true\n",
        ],
    )
    def test_create_rejects_junk_non_pipeline_bodies(
        self,
        def_manager: LocalWorkflowDefinitionManager,
        loader: WorkflowLoader,
        yaml_content: str,
    ) -> None:
        result = create_workflow_definition(def_manager, loader, yaml_content)

        assert result["success"] is False
        assert "extra_forbidden" in result["error"]

    @pytest.mark.parametrize(
        "yaml_content",
        [
            "name: missing-type\nsteps: []\n",
            "name: unsupported-type\ntype: workflow\nsteps: []\n",
            "name: malformed-type\ntype: [rule]\n",
        ],
    )
    def test_create_rejects_missing_or_unsupported_type(
        self,
        def_manager: LocalWorkflowDefinitionManager,
        loader: WorkflowLoader,
        yaml_content: str,
    ) -> None:
        result = create_workflow_definition(def_manager, loader, yaml_content)

        assert result["success"] is False
        assert "agent, pipeline, rule, variable" in result["error"]

    def test_create_rejects_agent_kind(self, loader: WorkflowLoader) -> None:
        result = create_workflow_definition(object(), loader, VALID_AGENT_YAML)  # type: ignore[arg-type]
        assert result["success"] is False
        assert "agent domain tools" in result["error"]

    def test_create_detects_name_conflict(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        # Create first
        result1 = create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)
        assert result1["success"] is True

        # Try to create duplicate
        result2 = create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)
        assert result2["success"] is False
        assert "already exists" in result2["error"]


# =============================================================================
# update_workflow_definition
# =============================================================================


class TestUpdateWorkflow:
    def test_update_by_name(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)

        result = update_workflow_definition(
            def_manager, loader, name="test-workflow", description="Updated desc"
        )

        assert result["success"] is True
        assert result["definition"]["description"] == "Updated desc"

    def test_update_by_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        created = create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)
        defn_id = created["definition"]["id"]

        result = update_workflow_definition(def_manager, loader, definition_id=defn_id, priority=25)

        assert result["success"] is True
        assert result["definition"]["priority"] == 25

    def test_update_multiple_fields(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)

        result = update_workflow_definition(
            def_manager,
            loader,
            name="test-workflow",
            description="New desc",
            enabled=False,
            priority=10,
            version="2.0",
            tags=["production"],
        )

        assert result["success"] is True
        defn = result["definition"]
        assert defn["description"] == "New desc"
        assert defn["enabled"] is False
        assert defn["priority"] == 10
        assert defn["version"] == "2.0"
        assert defn["tags"] == ["production"]

    def test_update_with_yaml_replacement(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)

        new_yaml = """\
name: test-workflow
description: Replaced definition
version: "3.0"
type: pipeline
steps:
  - id: new-step
    exec: echo new
"""
        result = update_workflow_definition(
            def_manager, loader, name="test-workflow", yaml_content=new_yaml
        )

        assert result["success"] is True
        assert result["definition"]["description"] == "Replaced definition"
        assert result["definition"]["version"] == "3.0"

    def test_update_validates_yaml(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)

        result = update_workflow_definition(
            def_manager, loader, name="test-workflow", yaml_content=INVALID_YAML_BAD_PIPELINE
        )

        assert result["success"] is False
        assert "YAML validation failed" in result["error"]

    def test_update_not_found(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        result = update_workflow_definition(
            def_manager, loader, name="nonexistent", description="x"
        )

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_update_no_fields(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)

        result = update_workflow_definition(def_manager, loader, name="test-workflow")

        assert result["success"] is False
        assert "No fields to update" in result["error"]

    def test_update_requires_name_or_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        result = update_workflow_definition(def_manager, loader, description="x")

        assert result["success"] is False
        assert "required" in result["error"]

    def test_update_yaml_replacement_rejects_step_type(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        """Regression: `type: step` used to silently rewrite workflow_type to
        'pipeline' via _LEGACY_TYPE_MAP. Now it must error."""
        create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)

        rogue_yaml = "name: test-workflow\ntype: step\nsteps:\n  - name: claim\n"
        result = update_workflow_definition(
            def_manager, loader, name="test-workflow", yaml_content=rogue_yaml
        )
        assert result["success"] is False
        assert "Invalid type" in result["error"]

    def test_update_rejects_type_change(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        create_workflow_definition(def_manager, loader, VALID_RULE_YAML)

        result = update_workflow_definition(
            def_manager,
            loader,
            name="test-rule",
            yaml_content=VALID_VARIABLE_YAML.replace("test-variable", "test-rule"),
        )

        assert result["success"] is False
        assert "does not match existing workflow type 'rule'" in result["error"]
        assert def_manager.get_by_name("test-rule").workflow_type == "rule"

    def test_update_without_type_validates_using_existing_type(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        create_workflow_definition(def_manager, loader, VALID_RULE_YAML)
        replacement = """\
name: test-rule
event: after_tool
effects:
  - type: observe
    category: test
"""

        result = update_workflow_definition(
            def_manager, loader, name="test-rule", yaml_content=replacement
        )

        assert result["success"] is True
        assert result["definition"]["workflow_type"] == "rule"

    def test_update_without_type_rejects_junk_for_existing_type(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        create_workflow_definition(def_manager, loader, VALID_AGENT_YAML)

        result = update_workflow_definition(
            def_manager,
            loader,
            name="test-agent",
            yaml_content="name: test-agent\nrole: replacement\njunk: true\n",
        )

        assert result["success"] is False
        assert "extra_forbidden" in result["error"]


# =============================================================================
# delete_workflow_definition
# =============================================================================


class TestDeleteWorkflow:
    def test_delete_by_name(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)

        result = delete_workflow_definition(def_manager, loader, name="test-workflow")

        assert result["success"] is True
        assert result["deleted"]["name"] == "test-workflow"
        assert def_manager.get_by_name("test-workflow") is None

    def test_delete_by_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        created = create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)
        defn_id = created["definition"]["id"]

        result = delete_workflow_definition(def_manager, loader, definition_id=defn_id)

        assert result["success"] is True
        assert result["deleted"]["id"] == defn_id

    def test_delete_bundled_protection(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        # Create a bundled definition with gobby tag
        def_manager.create(
            name="bundled-wf",
            definition_json=json.dumps({"name": "bundled-wf", "steps": [{"name": "work"}]}),
            tags=["gobby"],
        )

        result = delete_workflow_definition(def_manager, loader, name="bundled-wf")

        assert result["success"] is False
        assert "bundled" in result["error"]

    def test_delete_bundled_force(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        def_manager.create(
            name="bundled-wf",
            definition_json=json.dumps({"name": "bundled-wf", "steps": [{"name": "work"}]}),
            tags=["gobby"],
        )

        result = delete_workflow_definition(def_manager, loader, name="bundled-wf", force=True)

        assert result["success"] is True
        assert def_manager.get_by_name("bundled-wf") is None

    def test_delete_not_found(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        result = delete_workflow_definition(def_manager, loader, name="nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_delete_requires_name_or_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        result = delete_workflow_definition(def_manager, loader)

        assert result["success"] is False
        assert "required" in result["error"]


# =============================================================================
# export_workflow_definition
# =============================================================================


class TestExportWorkflow:
    def test_export_by_name(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)

        result = export_workflow_definition(def_manager, name="test-workflow")

        assert result["success"] is True
        assert result["name"] == "test-workflow"
        assert result["workflow_type"] == "pipeline"
        assert "name: test-workflow" in result["yaml_content"]

    def test_export_by_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        created = create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)
        defn_id = created["definition"]["id"]

        result = export_workflow_definition(def_manager, definition_id=defn_id)

        assert result["success"] is True
        assert isinstance(result["yaml_content"], str)

    def test_export_not_found(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        result = export_workflow_definition(def_manager, name="nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_export_returns_valid_yaml(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        import yaml

        create_workflow_definition(def_manager, loader, VALID_PIPELINE_YAML)

        result = export_workflow_definition(def_manager, name="test-pipeline")

        assert result["success"] is True
        data = yaml.safe_load(result["yaml_content"])
        assert data["name"] == "test-pipeline"
        assert data["type"] == "pipeline"

    def test_export_requires_name_or_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        result = export_workflow_definition(def_manager)

        assert result["success"] is False
        assert "required" in result["error"]


# =============================================================================
# Registry integration
# =============================================================================


class TestRegistryIntegration:
    def test_workflows_registry_has_crud_tools(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(db=db)
        tool_names = [t["name"] for t in registry.list_tools()]

        assert "create_workflow" in tool_names
        assert "update_workflow" in tool_names
        assert "delete_workflow" in tool_names
        assert "export_workflow" in tool_names
        create_tool = registry.get_tool_metadata("create_workflow")
        assert create_tool is not None
        assert "rule, variable, agent, or pipeline" in create_tool.description

    def test_pipelines_registry_has_crud_tools(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(db=db)
        tool_names = [t["name"] for t in registry.list_tools()]

        assert "get_pipeline" in tool_names
        assert "create_pipeline" in tool_names
        assert "update_pipeline" in tool_names
        assert "delete_pipeline" in tool_names
        assert "export_pipeline" in tool_names

    def test_workflows_crud_no_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry()
        tool_names = [t["name"] for t in registry.list_tools()]

        assert "create_workflow" in tool_names

    def test_pipelines_crud_no_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry()
        tool_names = [t["name"] for t in registry.list_tools()]

        assert "create_pipeline" in tool_names

    @pytest.mark.asyncio
    async def test_evaluate_workflow_uses_internal_mcp_inventory(self, monkeypatch) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.definitions import WorkflowDefinition, WorkflowStep

        class FakeInternalRegistry:
            name = "gobby-tasks"

            @staticmethod
            def list_tools() -> list[dict[str, str]]:
                return [{"name": "get_task"}]

        class FakeInternalManager:
            @staticmethod
            def list_servers() -> list[dict[str, object]]:
                return [{"name": "gobby-tasks"}]

            @staticmethod
            def get_all_registries() -> list[FakeInternalRegistry]:
                return [FakeInternalRegistry()]

        class FakeLoader:
            project_ids: list[str | None] = []

            @staticmethod
            async def load_workflow(name: str, project_id: str | None = None):
                FakeLoader.project_ids.append(project_id)
                return WorkflowDefinition(
                    name=name,
                    type="step",
                    steps=[
                        WorkflowStep(
                            name="start",
                            allowed_mcp_tools=["gobby-tasks:missing_tool"],
                        )
                    ],
                )

        project_id = "11111111-1111-4111-8111-111111111111"
        monkeypatch.setattr(
            "gobby.mcp_proxy.tools.workflows.get_project_context",
            lambda: {"id": project_id},
        )
        registry = create_workflows_registry(
            loader=FakeLoader(),
            internal_manager=FakeInternalManager(),
        )

        result = await registry.call("evaluate_workflow", {"name": "inventory-test"})

        codes = {item["code"] for item in result["items"]}
        assert "SEMANTIC_CHECKS_SKIPPED" not in codes
        assert "UNKNOWN_MCP_TOOL" in codes
        assert FakeLoader.project_ids == [project_id]

    async def test_evaluate_agent_definition_uses_project_scope(
        self,
        db: HubDatabase,
        def_manager: LocalWorkflowDefinitionManager,
        monkeypatch,
    ) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.definitions import AgentDefinitionBody

        project_id = "11111111-1111-4111-8111-111111110002"
        db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (project_id, "Agent Evaluation Project"),
        )
        def_manager.create(
            name="scoped-agent",
            definition_json=AgentDefinitionBody(
                name="global-agent",
                blocked_mcp_tools=["missing-server:missing-tool"],
            ).model_dump_json(),
            workflow_type="agent",
        )
        def_manager.create(
            name="scoped-agent",
            definition_json=AgentDefinitionBody(name="project-agent").model_dump_json(),
            workflow_type="agent",
            project_id=project_id,
        )
        monkeypatch.setattr(
            "gobby.mcp_proxy.tools.workflows.get_project_context",
            lambda: {"id": project_id},
        )
        registry = create_workflows_registry(db=db)

        result = await registry.call("evaluate_workflow", {"name": "scoped-agent"})

        assert result["valid"] is True
        assert result["workflow_name"] == "project-agent"
        assert result["workflow_type"] == "agent"
        assert all(item["code"] != "UNKNOWN_MCP_SERVER" for item in result["items"])


# =============================================================================
# No-database error paths
# =============================================================================


class TestNoDatabaseError:
    """CRUD tools return helpful errors when no database is connected."""

    @pytest.mark.asyncio
    async def test_create_workflow_no_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry()
        result = await registry.call("create_workflow", {"yaml_content": VALID_WORKFLOW_YAML})
        assert "error" in result
        assert "Definition tools require database connection" in result["error"]

    @pytest.mark.asyncio
    async def test_update_workflow_no_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry()
        result = await registry.call("update_workflow", {"name": "x", "description": "y"})
        assert "error" in result
        assert "Definition tools require database connection" in result["error"]

    @pytest.mark.asyncio
    async def test_delete_workflow_no_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry()
        result = await registry.call("delete_workflow", {"name": "x"})
        assert "error" in result
        assert "Definition tools require database connection" in result["error"]

    @pytest.mark.asyncio
    async def test_export_workflow_no_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry()
        result = await registry.call("export_workflow", {"name": "x"})
        assert "error" in result
        assert "Definition tools require database connection" in result["error"]

    @pytest.mark.asyncio
    async def test_create_pipeline_no_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry()
        result = await registry.call("create_pipeline", {"yaml_content": VALID_PIPELINE_YAML})
        assert "error" in result
        assert "Pipeline definition tools require database connection" in result["error"]

    @pytest.mark.asyncio
    async def test_update_pipeline_no_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry()
        result = await registry.call("update_pipeline", {"name": "x", "description": "y"})
        assert "error" in result
        assert "Pipeline definition tools require database connection" in result["error"]

    @pytest.mark.asyncio
    async def test_delete_pipeline_no_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry()
        result = await registry.call("delete_pipeline", {"name": "x"})
        assert "error" in result
        assert "Pipeline definition tools require database connection" in result["error"]

    @pytest.mark.asyncio
    async def test_export_pipeline_no_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry()
        result = await registry.call("export_pipeline", {"name": "x"})
        assert "error" in result
        assert "Pipeline definition tools require database connection" in result["error"]


# =============================================================================
# Pipeline type filtering
# =============================================================================


class TestPipelineTypeFiltering:
    """Pipeline CRUD wrappers reject non-pipeline definitions."""

    def test_update_pipeline_rejects_non_pipeline(
        self, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _require_pipeline

        # Create a non-pipeline definition directly
        def_manager.create(
            name="test-rule",
            definition_json='{"name": "test-rule", "event": "stop"}',
            workflow_type="rule",
            source="installed",
        )
        err = _require_pipeline(def_manager, name="test-rule")

        assert err is not None
        assert err["success"] is False
        assert "not a pipeline" in err["error"]

    def test_update_pipeline_accepts_pipeline(
        self, def_manager: LocalWorkflowDefinitionManager, loader: WorkflowLoader
    ) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _require_pipeline

        create_workflow_definition(def_manager, loader, VALID_PIPELINE_YAML)
        err = _require_pipeline(def_manager, name="test-pipeline")

        assert err is None

    def test_require_pipeline_not_found(
        self,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _require_pipeline

        err = _require_pipeline(def_manager, name="nonexistent")

        assert err is not None
        assert "not found" in err["error"]
