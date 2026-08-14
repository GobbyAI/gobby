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
from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
    WorkflowDefinitionRow,
)
from gobby.workflows.pipeline_loader import PipelineLoader

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
def loader(tmp_path) -> PipelineLoader:
    loader = PipelineLoader()
    loader.global_dirs = [tmp_path / "workflows"]
    return loader


def _seed_generic(
    def_manager: LocalWorkflowDefinitionManager, name: str = "test-workflow"
) -> WorkflowDefinitionRow:
    return def_manager.create(
        name=name,
        definition_json=json.dumps({"name": name, "steps": []}),
        workflow_type="workflow",
        source="installed",
    )


# =============================================================================
# create_workflow_definition
# =============================================================================


class TestCreateWorkflow:
    def test_create_rejects_variable_yaml_before_validation(self, loader: PipelineLoader) -> None:
        result = create_workflow_definition(
            object(),  # type: ignore[arg-type]
            loader,
            VALID_VARIABLE_YAML + "junk: true\n",
        )
        assert result["success"] is False
        assert "variable domain tools" in result["error"]

    def test_create_valid_workflow(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        result = create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)
        assert result["success"] is False
        assert "pipeline domain MCP tools" in result["error"]

    def test_create_valid_pipeline(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        result = create_workflow_definition(def_manager, loader, VALID_PIPELINE_YAML)
        assert result["success"] is False
        assert "pipeline domain MCP tools" in result["error"]

    def test_create_pipeline_normalizes_disabled_string(
        self, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        loader = PipelineLoader(def_manager.db)
        yaml_content = VALID_PIPELINE_YAML.replace(
            "type: pipeline", 'type: pipeline\nenabled: "false"'
        )
        result = create_workflow_definition(def_manager, loader, yaml_content)
        assert result["success"] is False
        assert "pipeline domain MCP tools" in result["error"]

    def test_create_with_project_id(
        self,
        db: HubDatabase,
        def_manager: LocalWorkflowDefinitionManager,
        loader: PipelineLoader,
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

        assert result["success"] is False
        assert "pipeline domain MCP tools" in result["error"]

    def test_create_rejects_invalid_yaml(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        result = create_workflow_definition(def_manager, loader, "not: [valid: yaml: {{")

        assert result["success"] is False
        assert "YAML parse error" in result["error"]

    def test_create_rejects_missing_name(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        result = create_workflow_definition(def_manager, loader, INVALID_YAML_NO_NAME)

        assert result["success"] is False
        assert "Validation failed" in result["error"]

    def test_create_rejects_pydantic_failures(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        result = create_workflow_definition(def_manager, loader, INVALID_YAML_BAD_PIPELINE)

        assert result["success"] is False
        assert "Validation failed" in result["error"]

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
        loader: PipelineLoader,
        yaml_content: str,
    ) -> None:
        result = create_workflow_definition(def_manager, loader, yaml_content)

        assert result["success"] is False
        assert "agent, pipeline, rule, variable" in result["error"]

    def test_create_rejects_agent_kind(self, loader: PipelineLoader) -> None:
        result = create_workflow_definition(object(), loader, VALID_AGENT_YAML)  # type: ignore[arg-type]
        assert result["success"] is False
        assert "agent domain tools" in result["error"]

    def test_create_rejects_rule_kind(self, loader: PipelineLoader) -> None:
        result = create_workflow_definition(object(), loader, VALID_RULE_YAML)  # type: ignore[arg-type]
        assert result["success"] is False
        assert "rule domain tools" in result["error"]

    def test_create_rejects_variable_kind(self, loader: PipelineLoader) -> None:
        result = create_workflow_definition(object(), loader, VALID_VARIABLE_YAML)  # type: ignore[arg-type]
        assert result["success"] is False
        assert "variable domain tools" in result["error"]

    def test_create_rejects_pipeline_kind(self, loader: PipelineLoader) -> None:
        result = create_workflow_definition(object(), loader, VALID_PIPELINE_YAML)  # type: ignore[arg-type]
        assert result["success"] is False
        assert "pipeline domain MCP tools" in result["error"]

    def test_create_detects_name_conflict(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager)
        result = create_workflow_definition(def_manager, loader, VALID_WORKFLOW_YAML)
        assert result["success"] is False
        assert "pipeline domain MCP tools" in result["error"]


# =============================================================================
# update_workflow_definition
# =============================================================================


class TestUpdateWorkflow:
    def test_update_by_name(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager)

        result = update_workflow_definition(
            def_manager, loader, name="test-workflow", description="Updated desc"
        )

        assert result["success"] is True
        assert result["definition"]["description"] == "Updated desc"

    def test_update_by_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        created = _seed_generic(def_manager)
        defn_id = created.id

        result = update_workflow_definition(def_manager, loader, definition_id=defn_id, priority=25)

        assert result["success"] is True
        assert result["definition"]["priority"] == 25

    def test_update_multiple_fields(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager)

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
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager)

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

        assert result["success"] is False
        assert "pipeline domain MCP tools" in result["error"]

    def test_update_validates_yaml(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager)

        result = update_workflow_definition(
            def_manager, loader, name="test-workflow", yaml_content=INVALID_YAML_BAD_PIPELINE
        )

        assert result["success"] is False
        assert "YAML validation failed" in result["error"]

    def test_update_not_found(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        result = update_workflow_definition(
            def_manager, loader, name="nonexistent", description="x"
        )

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_update_no_fields(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager)

        result = update_workflow_definition(def_manager, loader, name="test-workflow")

        assert result["success"] is False
        assert "No fields to update" in result["error"]

    def test_update_requires_name_or_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        result = update_workflow_definition(def_manager, loader, description="x")

        assert result["success"] is False
        assert "required" in result["error"]

    def test_update_yaml_replacement_rejects_step_type(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        """Regression: `type: step` used to silently rewrite workflow_type to
        'pipeline' via _LEGACY_TYPE_MAP. Now it must error."""
        _seed_generic(def_manager)

        rogue_yaml = "name: test-workflow\ntype: step\nsteps:\n  - name: claim\n"
        result = update_workflow_definition(
            def_manager, loader, name="test-workflow", yaml_content=rogue_yaml
        )
        assert result["success"] is False
        assert "Invalid or missing 'type'" in result["error"]

    def test_update_rejects_type_change(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager, name="test-pipeline")

        result = update_workflow_definition(
            def_manager,
            loader,
            name="test-pipeline",
            yaml_content=VALID_WORKFLOW_YAML.replace("test-workflow", "test-pipeline"),
        )

        assert result["success"] is False
        assert "pipeline domain MCP tools" in result["error"]

    def test_update_rejects_rule_kind(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager, name="test-pipeline")
        result = update_workflow_definition(
            def_manager, loader, name="test-pipeline", yaml_content=VALID_RULE_YAML
        )
        assert result["success"] is False
        assert "rule domain tools" in result["error"]

    def test_update_rejects_variable_kind(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager, name="test-pipeline")
        result = update_workflow_definition(
            def_manager, loader, name="test-pipeline", yaml_content=VALID_VARIABLE_YAML
        )
        assert result["success"] is False
        assert "variable domain tools" in result["error"]


# =============================================================================
# delete_workflow_definition
# =============================================================================


class TestDeleteWorkflow:
    def test_delete_by_name(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager)

        result = delete_workflow_definition(def_manager, loader, name="test-workflow")

        assert result["success"] is True
        assert result["deleted"]["name"] == "test-workflow"
        assert def_manager.get_by_name("test-workflow") is None

    def test_delete_by_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        created = _seed_generic(def_manager)
        defn_id = created.id

        result = delete_workflow_definition(def_manager, loader, definition_id=defn_id)

        assert result["success"] is True
        assert result["deleted"]["id"] == defn_id

    def test_delete_bundled_protection(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
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
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
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
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        result = delete_workflow_definition(def_manager, loader, name="nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_delete_requires_name_or_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        result = delete_workflow_definition(def_manager, loader)

        assert result["success"] is False
        assert "required" in result["error"]


# =============================================================================
# export_workflow_definition
# =============================================================================


class TestExportWorkflow:
    def test_export_by_name(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        _seed_generic(def_manager)

        result = export_workflow_definition(def_manager, name="test-workflow")

        assert result["success"] is True
        assert result["name"] == "test-workflow"
        assert result["workflow_type"] == "workflow"
        assert "name: test-workflow" in result["yaml_content"]

    def test_export_by_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        created = _seed_generic(def_manager)
        defn_id = created.id

        result = export_workflow_definition(def_manager, definition_id=defn_id)

        assert result["success"] is True
        assert isinstance(result["yaml_content"], str)

    def test_export_not_found(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        result = export_workflow_definition(def_manager, name="nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_export_returns_valid_yaml(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
    ) -> None:
        import yaml

        _seed_generic(def_manager, name="test-pipeline")

        result = export_workflow_definition(def_manager, name="test-pipeline")

        assert result["success"] is True
        data = yaml.safe_load(result["yaml_content"])
        assert data["name"] == "test-pipeline"

    def test_export_requires_name_or_id(
        self, def_manager: LocalWorkflowDefinitionManager, loader: PipelineLoader
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
            async def load_pipeline(name: str, project_id: str | None = None):
                FakeLoader.project_ids.append(project_id)
                return None

            def discover_pipelines_sync(self) -> list[object]:
                return []

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
        assert "WORKFLOW_NOT_FOUND" in codes
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
    """Pipeline CRUD wrappers resolve only typed pipeline rows."""

    def test_update_pipeline_rejects_non_pipeline(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _require_pipeline
        from gobby.storage.definitions.pipelines import PipelineDefinitionManager

        err = _require_pipeline(PipelineDefinitionManager(db), name="test-rule")
        assert err is not None
        assert err["success"] is False
        assert "not found" in err["error"]

    def test_update_pipeline_accepts_pipeline(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _require_pipeline
        from gobby.storage.definitions.pipelines import PipelineDefinitionManager

        manager = PipelineDefinitionManager(db)
        manager.create(
            name="test-pipeline",
            definition_json={
                "name": "test-pipeline",
                "type": "pipeline",
                "steps": [{"id": "s1", "exec": "echo hi"}],
            },
        )
        err = _require_pipeline(manager, name="test-pipeline")
        assert err is None

    def test_require_pipeline_not_found(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _require_pipeline
        from gobby.storage.definitions.pipelines import PipelineDefinitionManager

        err = _require_pipeline(PipelineDefinitionManager(db), name="nonexistent")
        assert err is not None
        assert "not found" in err["error"]
