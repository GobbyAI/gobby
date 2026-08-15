"""Tests for agent spawn dry-run evaluator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.agents.dry_run import SpawnEvaluation, evaluate_spawn
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentWorkflows,
    WorkflowDefinition,
    WorkflowStep,
    WorkflowTransition,
)
from gobby.workflows.dry_run import WorkflowEvaluation

pytestmark = pytest.mark.unit


def _setup_db(db: PostgresHubDatabase) -> PostgresHubDatabase:
    """Use the isolated typed-definition schema fixture."""
    return db


def _create_agent(
    db: PostgresHubDatabase,
    name: str = "test-agent",
    provider: str = "claude",
    isolation: str | None = None,
    pipeline: str | None = None,
    base_branch: str = "main",
    project_id: str | None = None,
) -> None:
    """Create an agent definition in the typed table."""
    body = AgentDefinitionBody(
        name=name,
        provider=provider,
        isolation=isolation,
        base_branch=base_branch,
        workflows=AgentWorkflows(pipeline=pipeline),
    )
    AgentDefinitionManager(db).create(
        name=name,
        definition_json=body.model_dump(mode="json"),
        source="installed",
        project_id=project_id,
    )


@pytest.fixture
def mock_workflow_loader() -> MagicMock:
    loader = MagicMock()
    loader.load_pipeline = AsyncMock(return_value=None)
    loader.validate_pipeline_for_agent = AsyncMock(return_value=(True, None))
    return loader


@pytest.fixture
def mock_runner() -> MagicMock:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "depth OK (0/3)", 0)
    return runner


class TestAgentNotFound:
    @pytest.mark.asyncio
    async def test_agent_not_found(self, definition_db: PostgresHubDatabase) -> None:
        """AGENT_NOT_FOUND error, can_spawn=False."""
        db = _setup_db(definition_db)

        result = await evaluate_spawn(
            agent="nonexistent",
            db=db,
        )

        assert result.can_spawn is False
        assert result.agent_found is False
        assert len(result.errors) == 1
        assert result.errors[0].code == "AGENT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_unrelated_project_agent_is_not_visible(
        self, definition_db: PostgresHubDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A same-name agent from another project must not leak into dry-run resolution."""
        db = _setup_db(definition_db)
        target_id = str(uuid4())
        unrelated_id = str(uuid4())
        _create_agent(db, project_id=unrelated_id)
        monkeypatch.setattr(
            "gobby.utils.project_context.get_project_context",
            lambda: {"id": target_id},
        )

        result = await evaluate_spawn(agent="test-agent", db=db)

        assert result.can_spawn is False
        assert result.agent_found is False
        assert len(result.errors) == 1
        assert result.errors[0].code == "AGENT_NOT_FOUND"


class TestWorkflowResolution:
    @pytest.mark.asyncio
    async def test_no_workflow(self, definition_db: PostgresHubDatabase) -> None:
        """NO_WORKFLOW info when no pipeline configured."""
        db = _setup_db(definition_db)
        _create_agent(db)

        result = await evaluate_spawn(agent="test-agent", db=db)

        no_wf = [i for i in result.items if i.code == "NO_WORKFLOW"]
        assert len(no_wf) == 1

    @pytest.mark.asyncio
    async def test_pipeline_resolved(
        self, definition_db: PostgresHubDatabase, mock_workflow_loader: MagicMock
    ) -> None:
        """WORKFLOW_RESOLVED when pipeline is configured."""
        db = _setup_db(definition_db)
        _create_agent(db, pipeline="my-pipeline")

        result = await evaluate_spawn(
            agent="test-agent",
            db=db,
            workflow_loader=mock_workflow_loader,
        )

        resolved = [i for i in result.items if i.code == "WORKFLOW_RESOLVED"]
        assert len(resolved) == 1
        assert result.effective_workflow == "my-pipeline"

    @pytest.mark.asyncio
    async def test_pipeline_warns_when_workflow_loader_is_unavailable(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        db = _setup_db(definition_db)
        _create_agent(db, pipeline="my-pipeline")

        result = await evaluate_spawn(agent="test-agent", db=db)

        skipped = [item for item in result.items if item.code == "WORKFLOW_VALIDATION_SKIPPED"]
        assert len(skipped) == 1
        assert skipped[0].level == "warning"

    @pytest.mark.asyncio
    async def test_explicit_workflow_overrides_pipeline(
        self, definition_db: PostgresHubDatabase, mock_workflow_loader: MagicMock
    ) -> None:
        """Explicit workflow parameter overrides agent's pipeline."""
        db = _setup_db(definition_db)
        _create_agent(db, pipeline="my-pipeline")

        result = await evaluate_spawn(
            agent="test-agent",
            workflow="explicit-wf",
            db=db,
            workflow_loader=mock_workflow_loader,
        )

        assert result.effective_workflow == "explicit-wf"


class TestIsolation:
    @pytest.mark.asyncio
    async def test_isolation_deps_missing_worktree(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        """ISOLATION_DEPS_MISSING for worktree mode without deps."""
        db = _setup_db(definition_db)
        _create_agent(db, isolation="worktree")

        result = await evaluate_spawn(
            agent="test-agent",
            db=db,
            git_manager=None,
            worktree_storage=None,
        )

        dep_items = [i for i in result.warnings if i.code == "ISOLATION_DEPS_MISSING"]
        assert len(dep_items) == 1

    @pytest.mark.asyncio
    async def test_isolation_deps_missing_clone(self, definition_db: PostgresHubDatabase) -> None:
        """ISOLATION_DEPS_MISSING for clone mode without deps."""
        db = _setup_db(definition_db)
        _create_agent(db, isolation="clone")

        result = await evaluate_spawn(
            agent="test-agent",
            db=db,
            clone_manager=None,
            clone_storage=None,
        )

        dep_items = [i for i in result.warnings if i.code == "ISOLATION_DEPS_MISSING"]
        assert len(dep_items) == 1


class TestRuntimeEnvironment:
    @pytest.mark.asyncio
    async def test_spawn_depth_exceeded(
        self, definition_db: PostgresHubDatabase, mock_runner: MagicMock
    ) -> None:
        """SPAWN_DEPTH_EXCEEDED when can_spawn returns False."""
        db = _setup_db(definition_db)
        _create_agent(db)
        mock_runner.can_spawn.return_value = (False, "Max depth 3 exceeded", 4)

        result = await evaluate_spawn(
            agent="test-agent",
            parent_session_id="sess-123",
            db=db,
            runner=mock_runner,
        )

        assert result.can_spawn is False
        depth_items = [i for i in result.errors if i.code == "SPAWN_DEPTH_EXCEEDED"]
        assert len(depth_items) == 1


class TestWorkflowEvaluation:
    @pytest.mark.asyncio
    async def test_workflow_eval_embedded(
        self,
        definition_db: PostgresHubDatabase,
        mock_workflow_loader: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """workflow_evaluation populated with structural results."""
        db = _setup_db(definition_db)
        _create_agent(db, pipeline="worker")

        wf_definition = WorkflowDefinition(
            name="worker",
            steps=[
                WorkflowStep(
                    name="work",
                    transitions=[WorkflowTransition(to="done", when="true")],
                ),
                WorkflowStep(name="done"),
            ],
        )
        mock_workflow_loader.load_pipeline.return_value = wf_definition
        project_id = "11111111-1111-4111-8111-111111111111"
        monkeypatch.setattr(
            "gobby.utils.project_context.get_project_context",
            lambda: {"id": project_id},
        )

        result = await evaluate_spawn(
            agent="test-agent",
            db=db,
            workflow_loader=mock_workflow_loader,
        )

        assert result.workflow_evaluation is not None
        assert result.workflow_evaluation.valid is True
        assert result.workflow_evaluation.step_trace == []
        assert any(
            item.code == "PIPELINE_TYPE" for item in result.workflow_evaluation.items
        )
        mock_workflow_loader.validate_pipeline_for_agent.assert_awaited_once_with(
            "worker", project_id
        )
        mock_workflow_loader.load_pipeline.assert_awaited_once_with("worker", project_id)

    @pytest.mark.asyncio
    async def test_explicit_project_path_overrides_ambient_workflow_scope(
        self,
        definition_db: PostgresHubDatabase,
        mock_workflow_loader: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An explicit target path determines workflow scoping for cross-project dry runs."""
        db = _setup_db(definition_db)
        mock_workflow_loader.load_pipeline.return_value = WorkflowDefinition(
            name="worker",
            steps=[WorkflowStep(name="done")],
        )
        ambient_id = "11111111-1111-4111-8111-111111111111"
        target_id = str(uuid4())
        _create_agent(db, pipeline="wrong-global-workflow")
        _create_agent(db, pipeline="worker", project_id=target_id)

        def fake_project_context(cwd: Path | None = None) -> dict[str, str]:
            return {"id": target_id} if cwd == tmp_path else {"id": ambient_id}

        monkeypatch.setattr(
            "gobby.utils.project_context.get_project_context",
            fake_project_context,
        )

        result = await evaluate_spawn(
            agent="test-agent",
            project_path=str(tmp_path),
            db=db,
            workflow_loader=mock_workflow_loader,
        )

        assert result.workflow_evaluation is not None
        assert result.workflow_evaluation.valid is True
        mock_workflow_loader.validate_pipeline_for_agent.assert_awaited_once_with(
            "worker", target_id
        )
        mock_workflow_loader.load_pipeline.assert_awaited_once_with("worker", target_id)

    @pytest.mark.asyncio
    async def test_workflow_invalid_for_agent(
        self, definition_db: PostgresHubDatabase, mock_workflow_loader: MagicMock
    ) -> None:
        """WORKFLOW_INVALID_FOR_AGENT when lifecycle workflow used for agent."""
        db = _setup_db(definition_db)
        _create_agent(db, pipeline="lifecycle-wf")

        mock_workflow_loader.validate_pipeline_for_agent.return_value = (
            False,
            "Cannot use lifecycle workflow",
        )

        result = await evaluate_spawn(
            agent="test-agent",
            db=db,
            workflow_loader=mock_workflow_loader,
        )

        assert result.can_spawn is False
        invalid_items = [i for i in result.errors if i.code == "WORKFLOW_INVALID_FOR_AGENT"]
        assert len(invalid_items) == 1


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_full_happy_path(
        self,
        definition_db: PostgresHubDatabase,
        mock_workflow_loader: MagicMock,
        mock_runner: MagicMock,
    ) -> None:
        """All layers pass, can_spawn=True."""
        db = _setup_db(definition_db)
        _create_agent(db, pipeline="worker")

        wf_definition = WorkflowDefinition(
            name="worker",
            steps=[
                WorkflowStep(
                    name="work",
                    transitions=[WorkflowTransition(to="done", when="true")],
                ),
                WorkflowStep(name="done"),
            ],
        )
        mock_workflow_loader.load_pipeline.return_value = wf_definition

        result = await evaluate_spawn(
            agent="test-agent",
            parent_session_id="sess-123",
            db=db,
            workflow_loader=mock_workflow_loader,
            runner=mock_runner,
        )

        assert result.can_spawn is True
        assert result.agent_found is True
        assert result.effective_workflow == "worker"
        assert len(result.errors) == 0


class TestToDict:
    def test_spawn_evaluation_to_dict(self) -> None:
        """SpawnEvaluation serializes correctly."""
        result = SpawnEvaluation(
            can_spawn=True,
            agent_name="test",
            agent_found=True,
            effective_provider="claude",
        )
        d = result.to_dict()
        assert d["can_spawn"] is True
        assert d["agent_name"] == "test"
        assert d["workflow_evaluation"] is None

    def test_spawn_evaluation_with_workflow_eval(self) -> None:
        """SpawnEvaluation with embedded workflow eval serializes correctly."""
        wf_eval = WorkflowEvaluation(valid=True, workflow_name="test")
        result = SpawnEvaluation(
            can_spawn=True,
            agent_name="test",
            workflow_evaluation=wf_eval,
        )
        d = result.to_dict()
        assert d["workflow_evaluation"] is not None
        assert d["workflow_evaluation"]["valid"] is True
