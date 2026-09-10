"""Production composition for one project-scoped native Ask service."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from gobby.ask.agents import AskRuntimeValidationLoader, ManagedAskAgents
from gobby.ask.evidence_runtime import AskSnapshotManager as AskSnapshotManagerProtocol
from gobby.ask.permissions import AskPermissionStore
from gobby.ask.runtime_validation import (
    AskRuntimeValidationArtifact,
    load_ask_runtime_validation,
)
from gobby.ask.service import AskService
from gobby.ask.snapshots import AskSnapshotManager
from gobby.ask.stages import AskStageStore
from gobby.ask.storage import AskRunStorage

if TYPE_CHECKING:
    from gobby.app_context import ServiceContainer


def build_ask_service(
    services: ServiceContainer,
    project_id: str,
    *,
    runtime_validation_artifacts: Mapping[str, AskRuntimeValidationArtifact],
    runtime_validation_loader: AskRuntimeValidationLoader = load_ask_runtime_validation,
) -> AskService | None:
    """Build Ask from the container's existing per-project infrastructure."""
    executor = services.get_pipeline_executor(project_id)
    if (
        executor is None
        or services.workflow_loader is None
        or services.worktree_storage is None
        or services.managed_credential_manager is None
        or services.agent_runner is None
        or services.session_manager is None
        or services.completion_registry is None
    ):
        return None
    pipeline = services.workflow_loader.load_pipeline_sync("native-ask", project_id)
    if pipeline is None:
        return None
    execution_manager = executor.execution_manager
    agent_runner = services.agent_runner
    storage = AskRunStorage(
        execution_manager,
        pipeline_snapshot=pipeline.model_dump(mode="json"),
    )

    async def cancel_agent(agent_run_id: str) -> None:
        await services.run_db(agent_runner.cancel_run, agent_run_id)

    config_runtime = services.config_runtime
    daemon_config = (
        config_runtime.capture().snapshot.active
        if config_runtime is not None and config_runtime.ready
        else None
    )
    agents = ManagedAskAgents(
        runner=agent_runner,
        db=services.database,
        session_manager=services.session_manager,
        completion_registry=services.completion_registry,
        runtime_validation_artifacts=runtime_validation_artifacts,
        cancel_agent=cancel_agent,
        daemon_config=daemon_config,
        runtime_validation_loader=runtime_validation_loader,
    )
    return AskService(
        storage=storage,
        stages=AskStageStore(execution_manager),
        snapshot_manager=cast(
            "AskSnapshotManagerProtocol",
            AskSnapshotManager(
                worktree_storage=services.worktree_storage,
                run_storage=storage,
                credential_manager=services.managed_credential_manager,
            ),
        ),
        agents=agents,
        permissions=AskPermissionStore(services.database),
        pipeline_executor=executor,
        state_root=None,
    )


__all__ = ["build_ask_service"]
