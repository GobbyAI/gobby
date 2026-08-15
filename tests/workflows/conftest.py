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
    from gobby.workflows.pipeline_loader import PipelineLoader


SYNTHETIC_SESSION_IDS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
)

SYNTHETIC_SESSION_TEST_MODULES = {
    "test_delivery_pipeline.py",
    "test_context_handoff_rules.py",
    "test_hook_evaluation_serialization.py",
    "test_hooks.py",
    "test_observers_detection.py",
    "test_review_learning_rules.py",
    "test_skill_loaded_call_tool_path.py",
    "test_task_enforcement_rules.py",
    "test_tool_context_rehydration.py",
}


@pytest.fixture(autouse=True)
def stable_workflow_machine_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep adapter tests isolated from the user's machine-id file."""
    monkeypatch.setattr(
        "gobby.adapters.codex_impl.app_server_adapter._get_daemon_machine_id",
        lambda: "workflow-tests",
    )


def _seed_synthetic_sessions(db: HubDatabase) -> None:
    """Seed parent rows for session-variable persistence tests."""
    from gobby.storage.projects import PERSONAL_PROJECT_ID

    db.executemany(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        VALUES (%s, %s, '21000000-0000-4000-8000-000000000001', 'test', %s)
        ON CONFLICT (id) DO NOTHING
        """,
        [(session_id, session_id, PERSONAL_PROJECT_ID) for session_id in SYNTHETIC_SESSION_IDS],
    )


@pytest.fixture(autouse=True)
def seed_hook_evaluation_sessions(request: pytest.FixtureRequest) -> None:
    """Seed synthetic sessions only for hook-evaluation regression modules."""
    uses_workflow_db = {"temp_db", "hub_db"}.intersection(request.fixturenames)
    if request.node.path.name in SYNTHETIC_SESSION_TEST_MODULES and uses_workflow_db:
        _seed_synthetic_sessions(request.getfixturevalue("postgres_db"))


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
def db_loader(workflow_db: HubDatabase) -> PipelineLoader:
    """Return a PipelineLoader backed by a DB with bundled workflows."""
    from gobby.workflows.pipeline_loader import PipelineLoader

    return PipelineLoader(db=workflow_db)


# Fixtures shared across the split test_pipeline_executor_*.py modules.
# test_mcp_step and test_pipeline_resume define module-local overrides for
# these names — pytest fixture resolution keeps those isolated.


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
    mock_step.approved_at = None
    manager.create_step_execution.return_value = mock_step
    manager.update_step_execution.return_value = mock_step
    manager.get_failed_steps.return_value = []
    return manager


@pytest.fixture
def mock_llm_service():
    """Create a mock LLM service.

    Uses MagicMock so callers can hang sync attributes off the service while
    LLM feature calls remain awaitable.
    """
    service = MagicMock()
    service.call_feature = AsyncMock(return_value="LLM response")
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
