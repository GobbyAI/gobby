"""Dynamic MCP tool registration for exposed pipelines."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.workflows._pipeline_execution import run_pipeline
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


def register_exposed_pipeline_tools(
    registry: InternalToolRegistry,
    loader: Any | None,
    pipeline_executor_resolver: Callable[[str], Any | None],
    session_manager: SessionManager | None = None,
    completion_registry: Any | None = None,
    db: HubDatabase | None = None,
    auto_subscribe_lineage: Callable[..., None] | None = None,
) -> None:
    """Register MCP tools for enabled pipelines marked ``expose_as_tool``."""
    if loader is None:
        logger.debug("Skipping dynamic pipeline tools: no loader")
        return

    try:
        discovered = loader.discover_pipelines_sync()
    except Exception:
        logger.warning("Failed to discover pipelines for dynamic tools", exc_info=True)
        return

    for workflow in discovered:
        pipeline = workflow.definition
        if not getattr(pipeline, "enabled", False):
            continue
        if not getattr(pipeline, "expose_as_tool", False):
            continue

        _create_pipeline_tool(
            registry,
            pipeline,
            loader,
            pipeline_executor_resolver,
            session_manager,
            completion_registry=completion_registry,
            db=db,
            auto_subscribe_lineage=auto_subscribe_lineage,
        )


def _create_pipeline_tool(
    registry: InternalToolRegistry,
    pipeline: Any,
    loader: Any,
    pipeline_executor_resolver: Callable[[str], Any | None],
    session_manager: SessionManager | None = None,
    completion_registry: Any | None = None,
    db: HubDatabase | None = None,
    auto_subscribe_lineage: Callable[..., None] | None = None,
) -> None:
    """Create a dynamic tool for a single pipeline."""
    tool_name = f"pipeline:{pipeline.name}"
    description = pipeline.description or f"Run the {pipeline.name} pipeline"
    input_schema = _build_input_schema(pipeline)
    pipeline_name = pipeline.name

    async def _execute_pipeline(**kwargs: Any) -> dict[str, Any]:
        kwargs.pop("session_id", None)
        continuation_prompt = kwargs.pop("continuation_prompt", None)

        resolved_id = get_current_session_id()
        if not resolved_id:
            return {"success": False, "error": "No session context available"}

        project_ctx = get_project_context()
        raw_project_id = project_ctx.get("id") if project_ctx else None
        if not isinstance(raw_project_id, str) or not raw_project_id:
            return {"success": False, "error": "No project context available"}

        executor = pipeline_executor_resolver(raw_project_id)
        if executor is None:
            return {
                "success": False,
                "error": f"Pipeline executor not available for project '{raw_project_id}'",
            }

        result = await run_pipeline(
            loader=loader,
            executor=executor,
            name=pipeline_name,
            inputs=kwargs,
            project_id=raw_project_id,
            session_id=resolved_id,
            continuation_prompt=continuation_prompt,
        )

        execution_id = result.get("execution_id")
        if (
            result.get("success")
            and execution_id
            and completion_registry
            and auto_subscribe_lineage
        ):
            auto_subscribe_lineage(
                completion_registry,
                execution_id,
                resolved_id,
                session_manager,
                continuation_prompt,
                db,
            )

        return result

    registry.register(
        name=tool_name,
        description=description,
        func=_execute_pipeline,
        input_schema=input_schema,
    )
    logger.debug("Registered dynamic pipeline tool: %s", tool_name)


def _build_input_schema(pipeline: Any) -> dict[str, Any]:
    """Build JSON Schema for pipeline inputs."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, input_def in pipeline.inputs.items():
        if isinstance(input_def, dict):
            prop = {"type": input_def.get("type", "string")}
            if "description" in input_def:
                prop["description"] = input_def["description"]
            if "default" in input_def:
                prop["default"] = input_def["default"]
            else:
                required.append(name)
            properties[name] = prop
        else:
            properties[name] = {"type": "string", "default": input_def}

    properties["continuation_prompt"] = {
        "type": "string",
        "description": (
            "Instructions for what to do when the pipeline completes. "
            "Included in the completion notification sent to subscribers."
        ),
    }

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
