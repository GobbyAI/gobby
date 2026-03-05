"""Pipeline discovery tools."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def list_pipelines(
    loader: Any,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    List available pipeline definitions.

    Args:
        loader: WorkflowLoader instance
        project_path: Optional project path for project-specific pipelines

    Returns:
        Dict with list of pipeline info or error
    """
    if not loader:
        return {"success": False, "error": "No loader configured"}

    try:
        discovered = await loader.discover_pipeline_workflows(project_path=project_path)

        pipelines = []
        for workflow in discovered:
            pipeline_info = {
                "name": workflow.name,
                "description": workflow.definition.description,
                "is_project": workflow.is_project,
                "path": str(workflow.path),
                "priority": workflow.priority,
            }

            # Add step count if available
            if hasattr(workflow.definition, "steps"):
                pipeline_info["step_count"] = len(workflow.definition.steps)

            pipelines.append(pipeline_info)

        return {
            "success": True,
            "pipelines": pipelines,
            "count": len(pipelines),
        }

    except (FileNotFoundError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception:
        logger.exception("Unexpected error discovering pipelines")
        return {"success": False, "error": "Internal error during pipeline discovery"}
