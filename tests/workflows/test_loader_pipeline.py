"""Tests for WorkflowLoader.load_pipeline() method.

DB-only tests: all definitions are inserted into the database via
LocalWorkflowDefinitionManager rather than written to YAML files.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody, PipelineDefinition, WorkflowDefinition
from gobby.workflows.loader import WorkflowLoader
from gobby.workflows.loader_validation import (
    _validate_fail_pipeline_completion_order,
    _validate_pipeline_references,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
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
    return LocalWorkflowDefinitionManager(db)


@pytest.fixture
def loader(db: HubDatabase) -> WorkflowLoader:
    return WorkflowLoader(db=db)


@pytest.fixture(scope="module")
def bundled_workflow_payload() -> Callable[[Path], dict[str, object]]:
    def load(path: Path) -> dict[str, object]:
        if not path.exists():
            pytest.skip(f"Bundled workflow not found: {path}")
        payload = yaml.safe_load(path.read_text())
        if not isinstance(payload, dict):
            raise AssertionError(f"Expected workflow mapping in {path}")
        return cast(dict[str, object], payload)

    return load


class TestLoadPipeline:
    """Tests for load_pipeline method."""

    @pytest.mark.asyncio
    async def test_load_valid_pipeline(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test loading a valid pipeline from DB returns PipelineDefinition."""
        def_manager.create(
            name="test-pipeline",
            definition_json=json.dumps(
                {
                    "name": "test-pipeline",
                    "type": "pipeline",
                    "description": "A test pipeline",
                    "steps": [
                        {"id": "step1", "exec": "echo hello"},
                        {"id": "step2", "exec": "echo world"},
                    ],
                }
            ),
            workflow_type="pipeline",
        )

        result = await loader.load_pipeline("test-pipeline")

        assert result is not None
        assert isinstance(result, PipelineDefinition)
        assert result.name == "test-pipeline"
        assert result.type == "pipeline"
        assert len(result.steps) == 2

    async def test_load_pipeline_uses_database_enabled_state(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Mutable row metadata overrides a stale enabled value in definition JSON."""
        row = def_manager.create(
            name="toggle-pipeline",
            definition_json=json.dumps(
                {
                    "name": "toggle-pipeline",
                    "type": "pipeline",
                    "enabled": True,
                    "steps": [{"id": "step1", "exec": "echo toggle"}],
                }
            ),
            workflow_type="pipeline",
            enabled=False,
        )

        disabled = await loader.load_pipeline("toggle-pipeline")

        assert disabled is not None
        assert disabled.enabled is False

        def_manager.update(row.id, enabled=True)
        loader.clear_cache()
        enabled = await loader.load_pipeline("toggle-pipeline")

        assert enabled is not None
        assert enabled.enabled is True

    @pytest.mark.parametrize(
        ("path", "step_id"),
        [
            (
                Path("src/gobby/install/shared/workflows/pipelines/spawn-developer.yaml"),
                "spawn",
            ),
            (Path("src/gobby/install/shared/workflows/dev.yaml"), "spawn_developer"),
        ],
    )
    def test_legacy_developer_dispatch_defaults_to_enabled_backend_agent(
        self,
        path: Path,
        step_id: str,
        bundled_workflow_payload: Callable[[Path], dict[str, object]],
    ) -> None:
        payload = bundled_workflow_payload(path)
        pipeline = PipelineDefinition.model_validate(payload)
        step = next(item for item in pipeline.steps if item.id == step_id)

        assert pipeline.inputs["agent"]["default"] == "backend-developer"
        assert step.mcp is not None
        assert step.mcp.arguments is not None
        assert step.mcp.arguments["agent"] == "${{ inputs.agent }}"

    def test_qa_dispatch_targets_enabled_reviewer_agent(
        self,
        bundled_workflow_payload: Callable[[Path], dict[str, object]],
    ) -> None:
        path = Path("src/gobby/install/shared/workflows/qa.yaml")
        payload = bundled_workflow_payload(path)
        pipeline = PipelineDefinition.model_validate(payload)
        step = next(item for item in pipeline.steps if item.id == "spawn_qa_reviewer")

        assert step.mcp is not None
        assert step.mcp.arguments is not None
        assert step.mcp.arguments["agent"] == "qa-reviewer"

    @pytest.mark.asyncio
    async def test_load_pipeline_not_found(self, loader: WorkflowLoader) -> None:
        """Test loading non-existent pipeline returns None."""
        result = await loader.load_pipeline("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_pipeline_wrong_type(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test loading a step workflow via load_pipeline returns None."""
        def_manager.create(
            name="step-workflow",
            definition_json=json.dumps(
                {
                    "name": "step-workflow",
                    "type": "step",
                    "steps": [{"name": "work", "allowed_tools": "all"}],
                }
            ),
            workflow_type="workflow",
        )

        result = await loader.load_pipeline("step-workflow")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_pipeline_project_path_priority(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
        project: Project,
    ) -> None:
        """Test that project-scoped pipelines take priority over global."""
        # Global pipeline
        def_manager.create(
            name="deploy",
            definition_json=json.dumps(
                {
                    "name": "deploy",
                    "type": "pipeline",
                    "steps": [{"id": "step1", "exec": "echo global"}],
                }
            ),
            workflow_type="pipeline",
        )

        # Project-scoped pipeline (should override)
        def_manager.create(
            name="deploy",
            definition_json=json.dumps(
                {
                    "name": "deploy",
                    "type": "pipeline",
                    "description": "Project-specific deploy",
                    "steps": [{"id": "step1", "exec": "echo project"}],
                }
            ),
            workflow_type="pipeline",
            project_id=project.id,
        )

        result = await loader.load_pipeline("deploy", project_path=project.id)

        assert result is not None
        assert result.description == "Project-specific deploy"

    @pytest.mark.asyncio
    async def test_load_pipeline_with_inputs_outputs(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test loading pipeline with inputs and outputs schema."""
        def_manager.create(
            name="parameterized",
            definition_json=json.dumps(
                {
                    "name": "parameterized",
                    "type": "pipeline",
                    "inputs": {
                        "files": {"type": "array", "description": "Files to process"},
                        "mode": {"type": "string", "default": "fast"},
                    },
                    "outputs": {"result": "$final.output"},
                    "steps": [{"id": "final", "exec": "echo done"}],
                }
            ),
            workflow_type="pipeline",
        )

        result = await loader.load_pipeline("parameterized")

        assert result is not None
        assert "files" in result.inputs
        assert "mode" in result.inputs
        assert result.inputs["mode"]["default"] == "fast"
        assert result.outputs["result"] == "$final.output"

    @pytest.mark.asyncio
    async def test_load_pipeline_with_approval(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test loading pipeline with approval gates."""
        def_manager.create(
            name="approval-pipeline",
            definition_json=json.dumps(
                {
                    "name": "approval-pipeline",
                    "type": "pipeline",
                    "steps": [
                        {"id": "test", "exec": "pytest"},
                        {
                            "id": "deploy",
                            "exec": "./deploy.sh",
                            "approval": {
                                "required": True,
                                "message": "Approve deployment?",
                            },
                        },
                    ],
                }
            ),
            workflow_type="pipeline",
        )

        result = await loader.load_pipeline("approval-pipeline")

        assert result is not None
        assert len(result.steps) == 2
        deploy_step = result.get_step("deploy")
        assert deploy_step is not None
        assert deploy_step.approval is not None
        assert deploy_step.approval.required is True
        assert deploy_step.approval.message == "Approve deployment?"

    @pytest.mark.asyncio
    async def test_load_pipeline_with_webhooks(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test loading pipeline with webhook configuration."""
        def_manager.create(
            name="webhook-pipeline",
            definition_json=json.dumps(
                {
                    "name": "webhook-pipeline",
                    "type": "pipeline",
                    "webhooks": {
                        "on_complete": {
                            "url": "https://example.com/done",
                            "method": "POST",
                        },
                    },
                    "steps": [{"id": "work", "exec": "echo working"}],
                }
            ),
            workflow_type="pipeline",
        )

        result = await loader.load_pipeline("webhook-pipeline")

        assert result is not None
        assert result.webhooks is not None
        assert result.webhooks.on_complete is not None
        assert result.webhooks.on_complete.url == "https://example.com/done"

    @pytest.mark.asyncio
    async def test_load_pipeline_inheritance(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test pipeline inheritance via extends field."""
        # Base pipeline
        def_manager.create(
            name="base-pipeline",
            definition_json=json.dumps(
                {
                    "name": "base-pipeline",
                    "type": "pipeline",
                    "inputs": {"env": {"type": "string", "default": "staging"}},
                    "steps": [{"id": "setup", "exec": "echo setup"}],
                }
            ),
            workflow_type="pipeline",
        )

        # Child pipeline extends base
        def_manager.create(
            name="child-pipeline",
            definition_json=json.dumps(
                {
                    "name": "child-pipeline",
                    "type": "pipeline",
                    "extends": "base-pipeline",
                    "inputs": {"env": {"default": "production"}},
                    "steps": [{"id": "deploy", "exec": "echo deploy"}],
                }
            ),
            workflow_type="pipeline",
        )

        result = await loader.load_pipeline("child-pipeline")

        assert result is not None
        assert result.name == "child-pipeline"
        # Should have inherited env input with overridden default
        assert result.inputs["env"]["default"] == "production"

    @pytest.mark.asyncio
    async def test_load_pipeline_caching(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that pipelines are cached after first load."""
        def_manager.create(
            name="cached-pipeline",
            definition_json=json.dumps(
                {
                    "name": "cached-pipeline",
                    "type": "pipeline",
                    "steps": [{"id": "step1", "exec": "echo cached"}],
                }
            ),
            workflow_type="pipeline",
        )

        # First load
        result1 = await loader.load_pipeline("cached-pipeline")
        # Second load should return cached version
        result2 = await loader.load_pipeline("cached-pipeline")

        assert result1 is result2  # Same object from cache

    @pytest.mark.asyncio
    async def test_load_pipeline_cache_invalidation(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that clear_cache forces a fresh reload from DB."""
        row = def_manager.create(
            name="cached-pipeline",
            definition_json=json.dumps(
                {
                    "name": "cached-pipeline",
                    "type": "pipeline",
                    "steps": [{"id": "step1", "exec": "echo cached"}],
                }
            ),
            workflow_type="pipeline",
        )

        # First load
        result1 = await loader.load_pipeline("cached-pipeline")
        assert result1 is not None
        assert result1.name == "cached-pipeline"

        # Clear cache
        loader.clear_cache()

        # Delete and re-insert with updated content
        def_manager.delete(row.id)
        def_manager.create(
            name="modified-pipeline",
            definition_json=json.dumps(
                {
                    "name": "modified-pipeline",
                    "type": "pipeline",
                    "steps": [{"id": "step1", "exec": "echo modified"}],
                }
            ),
            workflow_type="pipeline",
        )

        # Load the new one after cache cleared
        result2 = await loader.load_pipeline("modified-pipeline")
        assert result2 is not None
        assert result2.name == "modified-pipeline"
        assert result1 is not result2

    @pytest.mark.asyncio
    async def test_load_pipeline_invalid_json(
        self,
        loader: WorkflowLoader,
        db: HubDatabase,
    ) -> None:
        """Test that an invalid pipeline definition raises a load error."""
        with db.transaction() as conn:
            conn.execute(
                """INSERT INTO workflow_definitions
                   (id, name, workflow_type, enabled, priority,
                    definition_json, source, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    # workflow_definitions.id is a native uuid column
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "invalid",
                    "pipeline",
                    True,
                    100,
                    json.dumps("bad pipeline payload"),
                    "custom",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                ),
            )

        with pytest.raises(ValueError, match="Failed to parse DB workflow 'invalid'"):
            await loader.load_pipeline("invalid")

    @pytest.mark.asyncio
    async def test_load_pipeline_missing_type(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that a workflow without type=pipeline returns None from load_pipeline."""
        def_manager.create(
            name="no-type",
            definition_json=json.dumps(
                {
                    "name": "no-type",
                    "steps": [{"name": "work", "allowed_tools": "all"}],
                }
            ),
            workflow_type="workflow",
        )

        result = await loader.load_pipeline("no-type")
        # Should return None because it's not type: pipeline
        assert result is None


def _pipeline_references_are_valid(data: dict[str, Any]) -> bool:
    _validate_pipeline_references(data)
    return True


def _completion_order_is_valid(steps: list[dict[str, Any]]) -> bool:
    _validate_fail_pipeline_completion_order(steps)
    return True


class TestValidatePipelineReferences:
    """Tests for _validate_pipeline_references function.

    These test the standalone validation function directly, since errors raised
    during _load_from_db are caught and cause None to be returned.
    """

    def test_valid_back_reference(self) -> None:
        """Test that $earlier_step.output references are accepted."""
        data = {
            "name": "valid-refs",
            "type": "pipeline",
            "steps": [
                {"id": "step1", "exec": "echo hello"},
                {"id": "step2", "prompt": "Process the output from $step1.output"},
            ],
        }
        assert _pipeline_references_are_valid(data)

    def test_valid_back_reference_after_step_without_id(self) -> None:
        """An ID-less step does not shift physical reference positions."""
        data = {
            "name": "valid-refs-after-idless-step",
            "type": "pipeline",
            "steps": [
                {"id": "step1", "exec": "echo hello"},
                {"approval": {"prompt": "Continue?"}},
                {"id": "step2", "prompt": "Process $step1.output"},
            ],
        }
        assert _pipeline_references_are_valid(data)

    def test_rejects_forward_reference(self) -> None:
        """Test that $later_step.output (forward ref) is rejected."""
        data = {
            "name": "forward-ref",
            "type": "pipeline",
            "steps": [
                {"id": "step1", "prompt": "Use output from $step2.output"},
                {"id": "step2", "exec": "echo hello"},
            ],
        }
        with pytest.raises(ValueError) as exc_info:
            _validate_pipeline_references(data)
        assert "step2" in str(exc_info.value)
        assert "later" in str(exc_info.value).lower() or "forward" in str(exc_info.value).lower()

    def test_rejects_nonexistent_reference(self) -> None:
        """Test that $nonexistent.output is rejected."""
        data = {
            "name": "nonexistent-ref",
            "type": "pipeline",
            "steps": [
                {"id": "step1", "prompt": "Use output from $doesnotexist.output"},
            ],
        }
        with pytest.raises(ValueError) as exc_info:
            _validate_pipeline_references(data)
        assert "doesnotexist" in str(exc_info.value)

    def test_validates_prompt_references(self) -> None:
        """Test that references in prompt fields are validated."""
        data = {
            "name": "prompt-refs",
            "type": "pipeline",
            "steps": [
                {"id": "analyze", "exec": "./analyze.sh"},
                {
                    "id": "report",
                    "prompt": (
                        "Generate a report based on:\n"
                        "- Analysis: $analyze.output\n"
                        "- Summary: $analyze.output.summary"
                    ),
                },
            ],
        }
        assert _pipeline_references_are_valid(data)

    def test_validates_condition_references(self) -> None:
        """Test that references in condition fields are validated."""
        data = {
            "name": "condition-refs",
            "type": "pipeline",
            "steps": [
                {"id": "test", "exec": "pytest"},
                {
                    "id": "deploy",
                    "exec": "./deploy.sh",
                    "condition": "$test.output.exit_code == 0",
                },
            ],
        }
        assert _pipeline_references_are_valid(data)

    def test_validates_input_references(self) -> None:
        """Test that references in input fields are validated."""
        data = {
            "name": "input-refs",
            "type": "pipeline",
            "steps": [
                {"id": "step1", "exec": "echo start"},
                {"id": "step2", "exec": "process", "input": "$step1.output"},
            ],
        }
        assert _pipeline_references_are_valid(data)

    def test_validates_output_references(self) -> None:
        """Test that references in pipeline outputs are validated."""
        data = {
            "name": "output-refs",
            "type": "pipeline",
            "outputs": {
                "result": "$final.output",
                "summary": "$analyze.output.summary",
            },
            "steps": [
                {"id": "analyze", "exec": "./analyze.sh"},
                {"id": "final", "exec": "./finish.sh"},
            ],
        }
        assert _pipeline_references_are_valid(data)

    def test_rejects_invalid_output_reference(self) -> None:
        """Test that invalid references in outputs are rejected."""
        data = {
            "name": "bad-output-refs",
            "type": "pipeline",
            "outputs": {"result": "$missing_step.output"},
            "steps": [
                {"id": "step1", "exec": "echo hello"},
            ],
        }
        with pytest.raises(ValueError) as exc_info:
            _validate_pipeline_references(data)
        assert "missing_step" in str(exc_info.value)

    def test_allows_inputs_reference(self) -> None:
        """Test that $inputs.field references are allowed (not step refs)."""
        data = {
            "name": "inputs-ref",
            "type": "pipeline",
            "inputs": {"target": {"type": "string"}},
            "steps": [
                {"id": "step1", "exec": "echo $inputs.target"},
            ],
        }
        assert _pipeline_references_are_valid(data)

    def test_multiple_refs_all_valid(self) -> None:
        """Test pipeline with multiple valid references."""
        data = {
            "name": "multi-refs",
            "type": "pipeline",
            "steps": [
                {"id": "step1", "exec": "echo one"},
                {"id": "step2", "exec": "echo two"},
                {"id": "step3", "prompt": "Combine $step1.output and $step2.output"},
            ],
        }
        assert _pipeline_references_are_valid(data)

    def test_multiple_refs_one_invalid(self) -> None:
        """Test that one invalid ref among valid ones is caught."""
        data = {
            "name": "mixed-refs",
            "type": "pipeline",
            "steps": [
                {"id": "step1", "exec": "echo one"},
                {"id": "step2", "prompt": "Use $step1.output and $step3.output"},
                {"id": "step3", "exec": "echo three"},
            ],
        }
        with pytest.raises(ValueError) as exc_info:
            _validate_pipeline_references(data)
        assert "step3" in str(exc_info.value)

    def test_rejects_fail_pipeline_after_matching_completed_check(self) -> None:
        """_validate_pipeline_references rejects fail-run after completed-path.

        Step ordering matters because fail_pipeline validation treats that branch as too late.
        """
        data = {
            "name": "bad-validation-order",
            "type": "pipeline",
            "steps": [
                {"id": "wait_run", "exec": "echo wait"},
                {
                    "id": "completed-path",
                    "condition": "${{ steps.wait_run.output.status == 'completed' }}",
                    "exec": "echo ok",
                },
                {
                    "id": "fail-run",
                    "condition": "${{ steps.wait_run.output.status != 'completed' }}",
                    "mcp": {
                        "server": "gobby-workflows",
                        "tool": "fail_pipeline",
                        "arguments": {"message": "run failed"},
                    },
                },
            ],
        }

        with pytest.raises(ValueError, match="fail-run"):
            _validate_pipeline_references(data)

    def test_allows_fail_pipeline_before_matching_completed_check(self) -> None:
        """_validate_pipeline_references allows fail-run before completed-path.

        Step ordering matters because fail_pipeline validation sees the failure branch first.
        """
        data = {
            "name": "valid-validation-order",
            "type": "pipeline",
            "steps": [
                {"id": "wait_run", "exec": "echo wait"},
                {
                    "id": "fail-run",
                    "condition": "${{ steps.wait_run.output.status != 'completed' }}",
                    "mcp": {
                        "server": "gobby-workflows",
                        "tool": "fail_pipeline",
                        "arguments": {"message": "run failed"},
                    },
                },
                {
                    "id": "completed-path",
                    "condition": "${{ steps.wait_run.output.status == 'completed' }}",
                    "exec": "echo ok",
                },
            ],
        }

        assert _pipeline_references_are_valid(data)

    def test_fail_pipeline_completion_order_helper_rejects_matching_prior_success(self) -> None:
        """Direct helper rejects fail_pipeline after the matching completed branch."""
        steps: list[dict[str, Any]] = [
            {"id": "wait_run", "exec": "echo wait"},
            {
                "id": "completed-path",
                "condition": "${{ steps.wait_run.output.status == 'completed' }}",
                "exec": "echo ok",
            },
            {
                "id": "fail-run",
                "condition": "${{ steps.wait_run.output.status != 'completed' }}",
                "mcp": {
                    "server": "gobby-workflows",
                    "tool": "fail_pipeline",
                    "arguments": {"message": "run failed"},
                },
            },
        ]

        with pytest.raises(ValueError, match="fail-run"):
            _validate_fail_pipeline_completion_order(steps)

    def test_fail_pipeline_completion_order_helper_ignores_unmatched_output(self) -> None:
        """Direct helper allows fail_pipeline for a different output field."""
        steps: list[dict[str, Any]] = [
            {
                "id": "completed-path",
                "condition": "${{ steps.wait_run.output.status == 'completed' }}",
                "exec": "echo ok",
            },
            {
                "id": "fail-run",
                "condition": "${{ steps.wait_run.output.result != 'completed' }}",
                "mcp": {
                    "server": "gobby-workflows",
                    "tool": "fail_pipeline",
                    "arguments": {"message": "run failed"},
                },
            },
        ]

        assert _completion_order_is_valid(steps)


class TestLoadWorkflowPipelineIntegration:
    """Tests for load_workflow() auto-detecting and handling pipelines."""

    @pytest.mark.asyncio
    async def test_load_workflow_auto_detects_pipeline(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that load_workflow() auto-detects type=pipeline."""
        def_manager.create(
            name="auto-detect",
            definition_json=json.dumps(
                {
                    "name": "auto-detect",
                    "type": "pipeline",
                    "steps": [{"id": "step1", "exec": "echo hello"}],
                }
            ),
            workflow_type="pipeline",
        )

        result = await loader.load_workflow("auto-detect")

        assert result is not None
        assert isinstance(result, PipelineDefinition)
        assert result.type == "pipeline"

    @pytest.mark.asyncio
    async def test_load_workflow_treats_agent_definition_as_missing(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        agent = AgentDefinitionBody(name="agent-only")
        def_manager.create(
            name=agent.name,
            definition_json=agent.model_dump_json(),
            workflow_type="agent",
        )

        result = await loader.load_workflow(agent.name)

        assert result is None

    @pytest.mark.asyncio
    async def test_load_workflow_validates_pipeline_references(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that load_workflow() reports invalid pipeline references."""
        def_manager.create(
            name="validate-refs",
            definition_json=json.dumps(
                {
                    "name": "validate-refs",
                    "type": "pipeline",
                    "steps": [
                        {"id": "step1", "prompt": "Use $step2.output"},
                        {"id": "step2", "exec": "echo hello"},
                    ],
                }
            ),
            workflow_type="pipeline",
        )

        with pytest.raises(ValueError, match="appears later in the pipeline"):
            await loader.load_workflow("validate-refs")

    @pytest.mark.asyncio
    async def test_load_workflow_returns_workflow_definition_for_step(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that load_workflow() returns WorkflowDefinition for step type."""
        def_manager.create(
            name="step-workflow",
            definition_json=json.dumps(
                {
                    "name": "step-workflow",
                    "type": "step",
                    "steps": [{"name": "work", "allowed_tools": "all"}],
                }
            ),
            workflow_type="workflow",
        )

        result = await loader.load_workflow("step-workflow")

        assert result is not None
        assert isinstance(result, WorkflowDefinition)
        assert result.type == "step"

    @pytest.mark.asyncio
    async def test_load_workflow_returns_workflow_definition_for_lifecycle(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that load_workflow() returns WorkflowDefinition for lifecycle type."""
        def_manager.create(
            name="lifecycle-workflow",
            definition_json=json.dumps(
                {
                    "name": "lifecycle-workflow",
                    "type": "lifecycle",
                    "triggers": {"on_session_start": []},
                }
            ),
            workflow_type="workflow",
        )

        result = await loader.load_workflow("lifecycle-workflow")

        assert result is not None
        assert isinstance(result, WorkflowDefinition)
        assert result.type == "lifecycle"

    @pytest.mark.asyncio
    async def test_load_workflow_pipeline_with_valid_refs(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test load_workflow() succeeds for pipeline with valid references."""
        def_manager.create(
            name="valid-pipeline",
            definition_json=json.dumps(
                {
                    "name": "valid-pipeline",
                    "type": "pipeline",
                    "steps": [
                        {"id": "analyze", "exec": "./analyze.sh"},
                        {"id": "report", "prompt": "Generate report from $analyze.output"},
                    ],
                }
            ),
            workflow_type="pipeline",
        )

        result = await loader.load_workflow("valid-pipeline")

        assert result is not None
        assert isinstance(result, PipelineDefinition)
        assert len(result.steps) == 2


class TestDiscoverPipelineWorkflows:
    """Tests for discover_pipeline_workflows() method."""

    @pytest.mark.asyncio
    async def test_discovers_pipelines_in_global_dir(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that global pipelines (no project_id) are discovered."""
        def_manager.create(
            name="global-pipeline",
            definition_json=json.dumps(
                {
                    "name": "global-pipeline",
                    "type": "pipeline",
                    "steps": [{"id": "step1", "exec": "echo global"}],
                }
            ),
            workflow_type="pipeline",
        )

        result = await loader.discover_pipeline_workflows()

        assert len(result) == 1
        assert result[0].name == "global-pipeline"
        assert result[0].is_project is False
        assert isinstance(result[0].definition, PipelineDefinition)
        assert result[0].definition.type == "pipeline"

    async def test_discovery_excludes_disabled_pipelines(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        for name, enabled in (("enabled-pipeline", True), ("disabled-pipeline", False)):
            def_manager.create(
                name=name,
                definition_json=json.dumps(
                    {
                        "name": name,
                        "type": "pipeline",
                        "steps": [{"id": "step1", "exec": "echo test"}],
                    }
                ),
                workflow_type="pipeline",
                enabled=enabled,
            )

        result = await loader.discover_pipeline_workflows()

        assert [pipeline.name for pipeline in result] == ["enabled-pipeline"]

    async def test_discovery_uses_database_enabled_state(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """An enabled row must override stale disabled JSON during discovery."""
        def_manager.create(
            name="toggled-enabled-pipeline",
            definition_json=json.dumps(
                {
                    "name": "toggled-enabled-pipeline",
                    "type": "pipeline",
                    "enabled": False,
                    "expose_as_tool": True,
                    "steps": [{"id": "step1", "exec": "echo test"}],
                }
            ),
            workflow_type="pipeline",
            enabled=True,
        )

        result = await loader.discover_pipeline_workflows()

        assert len(result) == 1
        assert isinstance(result[0].definition, PipelineDefinition)
        assert result[0].definition.enabled is True
        assert result[0].definition.expose_as_tool is True

    @pytest.mark.asyncio
    async def test_discovers_pipelines_in_project_dir(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
        project: Project,
    ) -> None:
        """Test that project-scoped pipelines are discovered."""
        def_manager.create(
            name="project-pipeline",
            definition_json=json.dumps(
                {
                    "name": "project-pipeline",
                    "type": "pipeline",
                    "steps": [{"id": "step1", "exec": "echo project"}],
                }
            ),
            workflow_type="pipeline",
            project_id=project.id,
        )

        result = await loader.discover_pipeline_workflows(project_path=project.id)

        # Should find the project pipeline
        project_pipelines = [p for p in result if p.is_project]
        assert len(project_pipelines) == 1
        assert project_pipelines[0].name == "project-pipeline"

    @pytest.mark.parametrize("project_first", [False, True])
    @pytest.mark.asyncio
    async def test_project_shadows_global_pipeline(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        project_first: bool,
    ) -> None:
        """Test that project pipelines shadow global pipelines with same name."""
        # Global pipeline
        def_manager.create(
            name="deploy",
            definition_json=json.dumps(
                {
                    "name": "deploy",
                    "type": "pipeline",
                    "description": "Global deploy",
                    "steps": [{"id": "step1", "exec": "echo global"}],
                }
            ),
            workflow_type="pipeline",
        )

        # Project pipeline with same name
        def_manager.create(
            name="deploy",
            definition_json=json.dumps(
                {
                    "name": "deploy",
                    "type": "pipeline",
                    "description": "Project deploy",
                    "steps": [{"id": "step1", "exec": "echo project"}],
                }
            ),
            workflow_type="pipeline",
            project_id=project.id,
        )

        assert loader.def_manager is not None
        rows = loader.def_manager.list_all(project_id=project.id, workflow_type="pipeline")
        project_row = next(row for row in rows if row.project_id is not None)
        global_row = next(row for row in rows if row.project_id is None)
        ordered_rows = [project_row, global_row] if project_first else [global_row, project_row]
        monkeypatch.setattr(loader.def_manager, "list_all", lambda **_: ordered_rows)
        result = await loader.discover_pipeline_workflows(project_path=project.id)

        deploy_pipelines = [p for p in result if p.name == "deploy"]
        assert len(deploy_pipelines) == 1
        assert deploy_pipelines[0].is_project is True
        assert deploy_pipelines[0].definition.description == "Project deploy"

    @pytest.mark.asyncio
    async def test_ignores_non_pipeline_workflows(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that step/lifecycle workflows are not returned by discover_pipeline_workflows."""
        # Pipeline workflow
        def_manager.create(
            name="my-pipeline",
            definition_json=json.dumps(
                {
                    "name": "my-pipeline",
                    "type": "pipeline",
                    "steps": [{"id": "step1", "exec": "echo pipeline"}],
                }
            ),
            workflow_type="pipeline",
        )

        # Step workflow (should be ignored)
        def_manager.create(
            name="my-step",
            definition_json=json.dumps(
                {
                    "name": "my-step",
                    "type": "step",
                    "steps": [{"name": "work", "allowed_tools": "all"}],
                }
            ),
            workflow_type="workflow",
        )

        result = await loader.discover_pipeline_workflows()

        # Should only find the pipeline
        assert len(result) == 1
        assert result[0].name == "my-pipeline"
        assert isinstance(result[0].definition, PipelineDefinition)
        assert result[0].definition.type == "pipeline"

    @pytest.mark.asyncio
    async def test_returns_discovered_workflow_structure(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test that result has correct DiscoveredWorkflow structure."""
        def_manager.create(
            name="structured-pipeline",
            definition_json=json.dumps(
                {
                    "name": "structured-pipeline",
                    "type": "pipeline",
                    "steps": [{"id": "step1", "exec": "echo test"}],
                }
            ),
            workflow_type="pipeline",
            priority=50,
        )

        result = await loader.discover_pipeline_workflows()

        assert len(result) == 1
        discovered = result[0]
        # Check DiscoveredWorkflow fields
        assert discovered.name == "structured-pipeline"
        assert discovered.priority == 50
        assert discovered.is_project is False
        assert isinstance(discovered.definition, PipelineDefinition)

    @pytest.mark.asyncio
    async def test_discovers_multiple_pipelines(
        self,
        loader: WorkflowLoader,
        def_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        """Test discovering multiple pipelines."""
        for i in range(3):
            def_manager.create(
                name=f"pipeline-{i}",
                definition_json=json.dumps(
                    {
                        "name": f"pipeline-{i}",
                        "type": "pipeline",
                        "steps": [{"id": "step1", "exec": f"echo {i}"}],
                    }
                ),
                workflow_type="pipeline",
            )

        result = await loader.discover_pipeline_workflows()

        assert len(result) == 3
        names = {p.name for p in result}
        assert names == {"pipeline-0", "pipeline-1", "pipeline-2"}
