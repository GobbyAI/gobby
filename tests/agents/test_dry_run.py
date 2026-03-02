"""Tests for agent spawn dry-run evaluator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.dry_run import SpawnEvaluation, evaluate_spawn
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentWorkflows,
    WorkflowDefinition,
    WorkflowStep,
    WorkflowTransition,
)
from gobby.workflows.dry_run import WorkflowEvaluation

pytestmark = pytest.mark.unit


def _setup_db(tmp_path: Path) -> LocalDatabase:
    """Create a fresh database with migrations."""
    db = LocalDatabase(tmp_path / "test.db")
    run_migrations(db)
    return db


def _create_agent(
    db: LocalDatabase,
    name: str = "test-agent",
    mode: str = "autonomous",
    provider: str = "claude",
    isolation: str | None = None,
    pipeline: str | None = None,
    base_branch: str = "main",
) -> None:
    """Create an agent definition in the DB."""
    body = AgentDefinitionBody(
        name=name,
        mode=mode,
        provider=provider,
        isolation=isolation,
        base_branch=base_branch,
        workflows=AgentWorkflows(pipeline=pipeline),
    )
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="agent",
        source="template",
    )


@pytest.fixture
def mock_workflow_loader() -> MagicMock:
    loader = MagicMock()
    loader.load_workflow = AsyncMock(return_value=None)
    loader.validate_workflow_for_agent = AsyncMock(return_value=(True, None))
    return loader


@pytest.fixture
def mock_runner() -> MagicMock:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "depth OK (0/3)", 0)
    return runner


@pytest.fixture
def mock_state_manager() -> MagicMock:
    return MagicMock()


class TestAgentNotFound:
    @pytest.mark.asyncio
    async def test_agent_not_found(self, tmp_path: Path) -> None:
        """AGENT_NOT_FOUND error, can_spawn=False."""
        db = _setup_db(tmp_path)

        result = await evaluate_spawn(
            agent="nonexistent",
            db=db,
        )

        assert result.can_spawn is False
        assert result.agent_found is False
        assert len(result.errors) == 1
        assert result.errors[0].code == "AGENT_NOT_FOUND"


class TestWorkflowResolution:
    @pytest.mark.asyncio
    async def test_no_workflow(self, tmp_path: Path) -> None:
        """NO_WORKFLOW info when no pipeline configured."""
        db = _setup_db(tmp_path)
        _create_agent(db)

        result = await evaluate_spawn(agent="test-agent", db=db)

        no_wf = [i for i in result.items if i.code == "NO_WORKFLOW"]
        assert len(no_wf) == 1

    @pytest.mark.asyncio
    async def test_pipeline_resolved(
        self, tmp_path: Path, mock_workflow_loader: MagicMock
    ) -> None:
        """WORKFLOW_RESOLVED when pipeline is configured."""
        db = _setup_db(tmp_path)
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
    async def test_explicit_workflow_overrides_pipeline(
        self, tmp_path: Path, mock_workflow_loader: MagicMock
    ) -> None:
        """Explicit workflow parameter overrides agent's pipeline."""
        db = _setup_db(tmp_path)
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
    async def test_isolation_deps_missing_worktree(self, tmp_path: Path) -> None:
        """ISOLATION_DEPS_MISSING for worktree mode without deps."""
        db = _setup_db(tmp_path)
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
    async def test_isolation_deps_missing_clone(self, tmp_path: Path) -> None:
        """ISOLATION_DEPS_MISSING for clone mode without deps."""
        db = _setup_db(tmp_path)
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
        self, tmp_path: Path, mock_runner: MagicMock
    ) -> None:
        """SPAWN_DEPTH_EXCEEDED when can_spawn returns False."""
        db = _setup_db(tmp_path)
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

    @pytest.mark.asyncio
    async def test_self_mode_workflow_conflict(
        self, tmp_path: Path, mock_state_manager: MagicMock
    ) -> None:
        """SELF_MODE_WORKFLOW_CONFLICT when parent already has active workflow."""
        db = _setup_db(tmp_path)
        _create_agent(db, mode="autonomous")

        parent_state = MagicMock()
        parent_state.workflow_name = "existing-workflow"
        mock_state_manager.get_state.return_value = parent_state

        result = await evaluate_spawn(
            agent="test-agent",
            mode="self",
            parent_session_id="sess-123",
            db=db,
            state_manager=mock_state_manager,
        )

        conflict_items = [i for i in result.warnings if i.code == "SELF_MODE_WORKFLOW_CONFLICT"]
        assert len(conflict_items) == 1


class TestWorkflowEvaluation:
    @pytest.mark.asyncio
    async def test_workflow_eval_embedded(
        self, tmp_path: Path, mock_workflow_loader: MagicMock
    ) -> None:
        """workflow_evaluation populated with structural results."""
        db = _setup_db(tmp_path)
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
        mock_workflow_loader.load_workflow.return_value = wf_definition

        result = await evaluate_spawn(
            agent="test-agent",
            db=db,
            workflow_loader=mock_workflow_loader,
        )

        assert result.workflow_evaluation is not None
        assert result.workflow_evaluation.valid is True
        assert len(result.workflow_evaluation.step_trace) == 2

    @pytest.mark.asyncio
    async def test_workflow_invalid_for_agent(
        self, tmp_path: Path, mock_workflow_loader: MagicMock
    ) -> None:
        """WORKFLOW_INVALID_FOR_AGENT when lifecycle workflow used for agent."""
        db = _setup_db(tmp_path)
        _create_agent(db, pipeline="lifecycle-wf")

        mock_workflow_loader.validate_workflow_for_agent.return_value = (
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
        self, tmp_path: Path, mock_workflow_loader: MagicMock, mock_runner: MagicMock
    ) -> None:
        """All layers pass, can_spawn=True."""
        db = _setup_db(tmp_path)
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
        mock_workflow_loader.load_workflow.return_value = wf_definition

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
        wf_eval = WorkflowEvaluation(valid=True, workflow_name="test", workflow_type="step")
        result = SpawnEvaluation(
            can_spawn=True,
            agent_name="test",
            workflow_evaluation=wf_eval,
        )
        d = result.to_dict()
        assert d["workflow_evaluation"] is not None
        assert d["workflow_evaluation"]["valid"] is True
