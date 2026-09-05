"""Project purge construction and runtime datastore cleaner resolution."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from gobby.runner_init.services import mark_service_degraded

if TYPE_CHECKING:
    from gobby.projects.purge import GraphCleaner, ProjectStorage, VectorCleaner
    from gobby.runner import GobbyRunner
    from gobby.scheduler.executor import CronExecutor
    from gobby.storage.cron import CronJobStorage

logger = logging.getLogger(__name__)


def _resolve_project_vector_cleaner(runner: GobbyRunner) -> VectorCleaner:
    from gobby.projects.purge import NoopProjectVectorCleaner, ProjectPurgeVectorStoreUnavailable
    from gobby.projects.vector_cleanup import ProjectVectorCleaner

    bundle = runner.config_runtime.capture()
    memory = bundle.services.get("memory_services")
    vector_store = getattr(memory, "vector_store", None)
    if vector_store is not None:
        return ProjectVectorCleaner(vector_store)
    if bundle.snapshot.active.databases.qdrant.url is not None:
        raise ProjectPurgeVectorStoreUnavailable(
            "Qdrant is configured but the runtime memory bundle is unavailable"
        )
    return NoopProjectVectorCleaner()


def _resolve_project_graph_cleaner(runner: GobbyRunner) -> GraphCleaner:
    from gobby.config.persistence import is_falkordb_enabled
    from gobby.projects.purge import NoopProjectGraphCleaner

    bundle = runner.config_runtime.capture()
    memory = bundle.services.get("memory_services")
    manager = getattr(memory, "memory_manager", None)
    graph_cleaner = getattr(manager, "kg_service", None)
    if graph_cleaner is not None:
        return cast("GraphCleaner", graph_cleaner)
    if is_falkordb_enabled(bundle.snapshot.active.databases):
        raise RuntimeError("FalkorDB is configured but graph cleanup is unavailable")
    return NoopProjectGraphCleaner()


def init_project_purge(
    runner: GobbyRunner,
    projects: ProjectStorage,
    cron_storage: CronJobStorage,
    cron_executor: CronExecutor,
) -> None:
    """Register project cleanup against shared code, memory and graph services."""
    try:
        from gobby.code_index.gcode_gateway import GcodeGateway
        from gobby.projects.purge import ProjectPurgeService, register_project_purge_cron
        from gobby.storage.projects import GLOBAL_PROJECT_ID

        runner.project_purge_service = ProjectPurgeService(
            db=runner.database,
            projects=projects,
            cron=cron_storage,
            fence=runner.project_write_fence,
            code_gateway=GcodeGateway(),
            vector_cleaner=lambda: _resolve_project_vector_cleaner(runner),
            graph_cleaner=lambda: _resolve_project_graph_cleaner(runner),
            # The handshake factory attaches the maintenance launch factory to
            # the indexer after servers start; resolve it lazily at purge time.
            launch_factory=lambda: getattr(
                getattr(runner, "code_indexer", None), "launch_factory", None
            ),
        )
        register_project_purge_cron(
            cron_storage,
            cron_executor,
            runner.project_purge_service,
            project_id=GLOBAL_PROJECT_ID,
        )
        logger.debug("Project purge cron handler registered")
    except Exception:
        runner.project_purge_service = None
        mark_service_degraded(runner, "project_purge_service")
        logger.exception("Failed to initialize project purge service")
