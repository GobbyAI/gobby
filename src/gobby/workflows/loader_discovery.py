"""Workflow discovery functions for WorkflowLoader.

DB-only discovery — no filesystem scanning.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .definitions import PipelineDefinition, WorkflowDefinition
from .loader_cache import DiscoveredWorkflow, _CachedDiscovery

if TYPE_CHECKING:
    from .loader import WorkflowLoader

logger = logging.getLogger(__name__)


def _merge_db_workflows(
    loader: WorkflowLoader,
    discovered: dict[str, DiscoveredWorkflow],
    project_id: str | None = None,
) -> None:
    """Load DB workflow definitions into discovered dict."""
    mgr = loader.def_manager
    if mgr is None:
        return

    try:
        db_rows = mgr.list_all(project_id=project_id, workflow_type="workflow")
    except Exception as e:
        logger.warning(f"Failed to list DB workflow definitions: {e}")
        return

    for row in db_rows:
        try:
            data = json.loads(row.definition_json)
            if "type" in data and "enabled" not in data:
                data["enabled"] = data["type"] == "lifecycle"
            definition = WorkflowDefinition(**data)

            priority = row.priority
            is_project = row.project_id is not None
            existing = discovered.get(row.name)
            if existing is not None and existing.is_project and not is_project:
                continue
            discovered[row.name] = DiscoveredWorkflow(
                name=row.name,
                definition=definition,
                priority=priority,
                is_project=is_project,
                path=Path(f"db://{row.id}"),
            )
        except Exception as e:
            logger.warning(f"Failed to parse DB workflow '{row.name}': {e}")


def _merge_db_pipelines(
    loader: WorkflowLoader,
    discovered: dict[str, DiscoveredWorkflow],
    project_id: str | None = None,
) -> None:
    """Load DB pipeline definitions into discovered dict."""
    mgr = loader.def_manager
    if mgr is None:
        return

    try:
        db_rows = mgr.list_all(
            project_id=project_id,
            workflow_type="pipeline",
            enabled=True,
        )
    except Exception as e:
        logger.warning(f"Failed to list DB pipeline definitions: {e}")
        return

    for row in db_rows:
        try:
            data = json.loads(row.definition_json)
            # Mutable row metadata is authoritative; definition_json may still
            # contain the value from import time after a later enable toggle.
            data["enabled"] = row.enabled
            loader._validate_pipeline_references(data)
            definition = PipelineDefinition(**data)

            priority = row.priority
            is_project = row.project_id is not None
            existing = discovered.get(row.name)
            if existing is not None and existing.is_project and not is_project:
                continue
            discovered[row.name] = DiscoveredWorkflow(
                name=row.name,
                definition=definition,
                priority=priority,
                is_project=is_project,
                path=Path(f"db://{row.id}"),
            )
        except Exception as e:
            logger.warning(f"Failed to parse DB pipeline '{row.name}': {e}")


async def discover_workflows(
    loader: WorkflowLoader, project_path: Path | str | None = None
) -> list[DiscoveredWorkflow]:
    """
    Discover all workflows from the database.

    Returns workflows sorted by:
    1. Project workflows first (is_project=True), then global
    2. Within each group: by priority (ascending), then alphabetically by name

    Project workflows shadow global workflows with the same name.
    """
    project_id = loader._db_project_scope(project_path, label="workflow discovery")
    cache_key = f"unified:{project_id}" if project_id else "unified:global"

    # Check cache
    if cache_key in loader._discovery_cache:
        cached = loader._discovery_cache[cache_key]
        return cached.results

    discovered: dict[str, DiscoveredWorkflow] = {}

    # DB-only: load all workflow definitions
    _merge_db_workflows(loader, discovered, project_id=project_id)

    # Sort: project first, then by priority (asc), then by name (alpha)
    sorted_workflows = sorted(
        discovered.values(),
        key=lambda w: (
            0 if w.is_project else 1,
            w.priority,
            w.name,
        ),
    )

    # Cache and return
    loader._discovery_cache[cache_key] = _CachedDiscovery(results=sorted_workflows)
    return sorted_workflows


async def discover_pipeline_workflows(
    loader: WorkflowLoader, project_path: Path | str | None = None
) -> list[DiscoveredWorkflow]:
    """
    Discover all pipeline workflows from the database.

    Returns pipelines sorted by:
    1. Project pipelines first (is_project=True), then global
    2. Within each group: by priority (ascending), then alphabetically by name

    Project pipelines shadow global pipelines with the same name.
    """
    project_id = loader._db_project_scope(project_path, label="pipeline discovery")
    cache_key = f"pipelines:{project_id}" if project_id else "pipelines:global"

    # Check cache
    if cache_key in loader._discovery_cache:
        cached = loader._discovery_cache[cache_key]
        return cached.results

    discovered: dict[str, DiscoveredWorkflow] = {}

    # DB-only: load all pipeline definitions
    _merge_db_pipelines(loader, discovered, project_id=project_id)

    # Sort: project first, then by priority (asc), then by name (alpha)
    sorted_pipelines = sorted(
        discovered.values(),
        key=lambda w: (
            0 if w.is_project else 1,
            w.priority,
            w.name,
        ),
    )

    # Cache and return
    loader._discovery_cache[cache_key] = _CachedDiscovery(results=sorted_pipelines)
    return sorted_pipelines
