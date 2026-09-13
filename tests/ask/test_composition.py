from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from gobby.app_context import ServiceContainer
from gobby.ask.composition import build_ask_service
from gobby.ask.pipeline import parse_ask_pipeline

pytestmark = pytest.mark.unit


@pytest.fixture
def ask_services() -> ServiceContainer:
    project_id = "project-1"
    pipeline_path = (
        Path(__file__).parents[2] / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
    )
    pipeline = parse_ask_pipeline(yaml.safe_load(pipeline_path.read_text(encoding="utf-8")))
    execution_manager = MagicMock()
    executor = SimpleNamespace(execution_manager=execution_manager)
    workflow_loader = MagicMock()
    workflow_loader.load_pipeline_sync.return_value = pipeline
    return ServiceContainer(
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


def test_build_ask_service_reuses_container_pipeline_infrastructure(
    ask_services: ServiceContainer,
) -> None:
    service = build_ask_service(ask_services, "project-1", runtime_validation_artifacts={})

    assert service is not None
    assert service.pipeline_executor is ask_services.pipeline_executor
    assert service.storage.manager is ask_services.pipeline_executor.execution_manager
    assert service.stages.manager is ask_services.pipeline_executor.execution_manager
    assert ask_services.workflow_loader is not None
    ask_services.workflow_loader.load_pipeline_sync.assert_called_once_with(
        "native-ask", "project-1"
    )


@pytest.mark.parametrize("success", [True, False])
async def test_cancel_ask_agent_uses_process_and_completion_lifecycle(
    ask_services: ServiceContainer, success: bool
) -> None:
    service = build_ask_service(ask_services, "project-1", runtime_validation_artifacts={})
    assert service is not None
    runner = ask_services.agent_runner
    assert runner is not None
    with patch(
        "gobby.mcp_proxy.tools.agent_cancellation.terminate_agent_run",
        new_callable=AsyncMock,
        return_value={"success": success, "error": "process still alive"},
    ) as terminate:
        if success:
            await service.agents.cancel("ask-agent")
        else:
            with pytest.raises(RuntimeError, match="process still alive"):
                await service.agents.cancel("ask-agent")

        terminate.assert_awaited_once_with(
            run=runner.get_run.return_value,
            runner=runner,
            agent_run_manager=runner.run_storage,
            db=ask_services.database,
            lifecycle_monitor=ask_services.agent_lifecycle_monitor,
            completion_registry=ask_services.completion_registry,
            task_manager=ask_services.task_manager,
            session_manager=ask_services.session_manager,
            effective_status="cancelled",
        )
        runner.cancel_run.assert_not_called()


def test_build_ask_service_fails_closed_without_shared_executor() -> None:
    services = ServiceContainer(
        database=MagicMock(),
        session_manager=MagicMock(),
        task_manager=MagicMock(),
        project_id="project-1",
    )

    assert build_ask_service(services, "project-1", runtime_validation_artifacts={}) is None
