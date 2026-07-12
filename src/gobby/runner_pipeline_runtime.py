"""Construction helpers for project-scoped pipeline runtimes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.workflows.pipeline_executor import PipelineExecutor


def build_pipeline_runtime(
    runner: GobbyRunner,
    project_id: str,
) -> tuple[LocalPipelineExecutionManager, PipelineExecutor]:
    """Build an isolated execution manager and executor for one project."""
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.workflows.templates import TemplateEngine

    manager = LocalPipelineExecutionManager(db=runner.database, project_id=project_id)
    executor = PipelineExecutor(
        db=runner.database,
        execution_manager=manager,
        llm_service=runner.llm_service,
        loader=runner.workflow_loader,
        template_engine=TemplateEngine(),
        session_manager=runner.session_manager,
        completion_registry=runner.completion_registry,
        run_db=runner.db_executor.run,
    )
    return manager, executor
