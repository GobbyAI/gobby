"""Shared fixtures for workflow tests."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.workflows.loader import WorkflowLoader


@pytest.fixture
def workflow_db(hub_db: HubDatabase) -> Iterator[HubDatabase]:
    """Populate a module-scoped DB with bundled workflows and return it.

    Uses the PostgreSQL test hub; tests receive a reset schema from the shared
    postgres fixture stack.
    """
    from gobby.workflows.sync_pipelines import sync_bundled_pipelines

    sync_bundled_pipelines(hub_db)
    yield hub_db


@pytest.fixture
def db_loader(workflow_db: HubDatabase) -> WorkflowLoader:
    """Return a WorkflowLoader backed by a DB with bundled workflows."""
    from gobby.workflows.loader import WorkflowLoader

    return WorkflowLoader(db=workflow_db)


# Fixtures shared across the split test_pipeline_executor_*.py modules.
# Other workflow test modules (test_mcp_step, test_pipeline_resume,
# test_summary_actions, test_webhook_executor) define module-local overrides
# for these names — pytest fixture resolution keeps those isolated.


@pytest.fixture
def mock_db():
    """Create a mock database."""
    return MagicMock()


@pytest.fixture
def mock_execution_manager():
    """Create a mock LocalPipelineExecutionManager."""
    manager = MagicMock()
    mock_execution = MagicMock()
    mock_execution.id = "pe-test-123"
    mock_execution.status = ExecutionStatus.PENDING
    manager.create_execution.return_value = mock_execution
    manager.get_execution.return_value = mock_execution
    manager.update_execution_status.return_value = mock_execution
    mock_step = MagicMock()
    mock_step.id = 1
    mock_step.status = StepStatus.PENDING
    manager.create_step_execution.return_value = mock_step
    manager.update_step_execution.return_value = mock_step
    manager.get_failed_steps.return_value = []
    return manager


@pytest.fixture
def mock_llm_service():
    """Create a mock LLM service.

    Uses MagicMock (not AsyncMock) because get_default_provider() is sync.
    The provider itself is AsyncMock since generate_text() is async.
    """
    service = MagicMock()
    mock_provider = AsyncMock()
    mock_provider.generate_text.return_value = "LLM response"
    service.get_default_provider.return_value = mock_provider
    return service


@pytest.fixture
def mock_template_engine():
    """Create a mock template engine."""
    engine = MagicMock()
    engine.render.side_effect = lambda template, context: template
    return engine


@pytest.fixture
def mock_webhook_notifier():
    """Create a mock webhook notifier."""
    return AsyncMock()


@pytest.fixture
def simple_pipeline():
    """Create a simple pipeline definition."""
    return PipelineDefinition(
        name="test-pipeline",
        description="A test pipeline",
        steps=[
            PipelineStep(id="step1", exec="echo hello"),
            PipelineStep(id="step2", exec="echo world"),
        ],
    )


@pytest.fixture
def pipeline_with_prompt():
    """Create a pipeline with a prompt step."""
    return PipelineDefinition(
        name="prompt-pipeline",
        steps=[
            PipelineStep(id="analyze", exec="echo analyzing"),
            PipelineStep(id="report", prompt="Generate report from $analyze.output"),
        ],
    )


@pytest.fixture
def pipeline_with_inputs():
    """Create a pipeline with inputs."""
    return PipelineDefinition(
        name="input-pipeline",
        inputs={
            "target": {"type": "string", "description": "Target to process"},
            "mode": {"type": "string", "default": "fast"},
        },
        steps=[
            PipelineStep(id="process", exec="echo processing"),
        ],
    )
