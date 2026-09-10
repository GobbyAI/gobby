from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from gobby.app_context import ServiceContainer
from gobby.ask.composition import build_ask_service
from gobby.ask.pipeline import parse_ask_pipeline

pytestmark = pytest.mark.unit


def test_build_ask_service_reuses_container_pipeline_infrastructure() -> None:
    project_id = "project-1"
    pipeline_path = (
        Path(__file__).parents[2] / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
    )
    pipeline = parse_ask_pipeline(yaml.safe_load(pipeline_path.read_text(encoding="utf-8")))
    execution_manager = MagicMock()
    executor = SimpleNamespace(execution_manager=execution_manager)
    workflow_loader = MagicMock()
    workflow_loader.load_pipeline_sync.return_value = pipeline
    services = ServiceContainer(
        database=MagicMock(),
        session_manager=MagicMock(),
        task_manager=MagicMock(),
        project_id=project_id,
        pipeline_executor=executor,
        workflow_loader=workflow_loader,
        worktree_storage=MagicMock(),
        managed_credential_manager=MagicMock(),
        agent_runner=MagicMock(),
        completion_registry=MagicMock(),
    )

    service = build_ask_service(services, project_id, runtime_validation_artifacts={})

    assert service is not None
    assert service.pipeline_executor is executor
    assert service.storage.manager is execution_manager
    assert service.stages.manager is execution_manager
    workflow_loader.load_pipeline_sync.assert_called_once_with("native-ask", project_id)


def test_build_ask_service_fails_closed_without_shared_executor() -> None:
    services = ServiceContainer(
        database=MagicMock(),
        session_manager=MagicMock(),
        task_manager=MagicMock(),
        project_id="project-1",
    )

    assert build_ask_service(services, "project-1", runtime_validation_artifacts={}) is None
