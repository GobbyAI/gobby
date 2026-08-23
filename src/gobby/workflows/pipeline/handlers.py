"""Step execution handlers for pipeline workflows."""

import asyncio
import logging
import shlex
from typing import TYPE_CHECKING, Any

from mcp.types import CallToolResult, TextContent

from gobby.config.feature_base import FeatureDefaultConfig

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


async def execute_mcp_step(
    rendered_step: Any,
    context: dict[str, Any],
    tool_proxy_getter: Any | None,
    session_manager: "SessionManager | None" = None,
) -> Any:
    """Execute an MCP tool call step."""
    mcp_config = rendered_step.mcp

    logger.info("Executing MCP step: %s:%s", mcp_config.server, mcp_config.tool)

    if not tool_proxy_getter:
        raise RuntimeError(
            f"MCP step {rendered_step.id} requires tool_proxy_getter but none configured"
        )

    tool_proxy = tool_proxy_getter()
    if not tool_proxy:
        raise RuntimeError("tool_proxy_getter returned None")

    # Set project + session context for pipeline MCP steps via the shared
    # helper. Resolves external_id refs to the platform UUID and propagates
    # it to tool_proxy — otherwise the proxy would prefer the raw ref over
    # the ContextVar and re-poison workflow checks and tool filters.
    from gobby.utils.session_context import (
        reset_seeded_contexts,
        resolve_and_seed_contexts,
    )

    pipeline_session_id = context.get("session_id")
    tokens = await resolve_and_seed_contexts(
        session_ref=pipeline_session_id,
        session_manager=session_manager,
        project_ref=context.get("project_id"),
        session_ref_origin="ambient",
        project_ref_is_fallback=True,
        db=(session_manager.db if session_manager else None),
    )
    effective_session_id = tokens.resolved_session_id
    try:
        if pipeline_session_id:
            # Internal pipeline execution still needs to satisfy progressive
            # discovery rules before calling the tool.
            try:
                await tool_proxy.get_tool_schema(
                    mcp_config.server,
                    mcp_config.tool,
                    session_id=effective_session_id,
                )
            except Exception as schema_err:
                logger.debug(
                    "Failed to prefetch schema for pipeline MCP step %s:%s: %s",
                    mcp_config.server,
                    mcp_config.tool,
                    schema_err,
                )

        result = await tool_proxy.call_tool(
            mcp_config.server,
            mcp_config.tool,
            mcp_config.arguments or {},
            session_id=effective_session_id,
        )
    finally:
        reset_seeded_contexts(tokens)

    # Downstream (external) servers answer with the SDK CallToolResult; it
    # fails closed on is_error before anything flows to later steps.
    if isinstance(result, CallToolResult):
        output = "\n".join(item.text for item in result.content if isinstance(item, TextContent))
        if result.is_error:
            raise RuntimeError(
                f"MCP step {rendered_step.id} failed: "
                f"{mcp_config.server}:{mcp_config.tool} returned error: {output}"
            )
        converted: dict[str, Any] = {"result": output}
        if result.structured_content is not None:
            converted["structured_content"] = result.structured_content
        return converted

    # Check for MCP-level failure (dict responses from internal tools)
    # Supports both old pattern (success=False) and new pattern (error key only)
    if isinstance(result, dict) and (
        result.get("success") is False or ("error" in result and result.get("success") is not True)
    ):
        error_msg = result.get("error", "Unknown MCP tool error")
        raise RuntimeError(
            f"MCP step {rendered_step.id} failed: "
            f"{mcp_config.server}:{mcp_config.tool} returned error: {error_msg}"
        )

    # Strip redundant success field so step outputs are clean data dicts
    if isinstance(result, dict) and "success" in result:
        result = {k: v for k, v in result.items() if k != "success"}

    return result


async def execute_exec_step(command: str, context: dict[str, Any]) -> dict[str, Any]:
    """Execute a shell command step.

    Commands are parsed using shlex.split and executed via create_subprocess_exec
    to avoid shell injection vulnerabilities.  A configurable timeout (default
    300 s) is read from ``context["timeout_seconds"]``.
    """
    timeout_seconds: float = context.get("timeout_seconds", 300)
    logger.info("Executing command: %s", command)

    try:
        args = shlex.split(command)
        if not args:
            return {
                "stdout": "",
                "stderr": "Empty command",
                "exit_code": 1,
            }

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "stdout": "",
                "stderr": f"Command timed out after {timeout_seconds}s",
                "exit_code": -1,
            }

        return {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode,
        }
    except (OSError, ValueError) as e:
        logger.exception("Command execution failed: %s", e)
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": 1,
        }


async def execute_prompt_step(
    prompt: str, context: dict[str, Any], llm_service: Any, feature_config: FeatureDefaultConfig
) -> dict[str, Any]:
    """Execute an LLM prompt step."""
    if not llm_service:
        return {"error": "prompt step requires llm_service but none configured"}

    try:
        response = await llm_service.call_feature(
            feature_config,
            prompt,
            caller="workflows.pipeline.prompt_step",
        )
        return {"response": response}
    except (OSError, RuntimeError, ValueError) as e:
        logger.exception("LLM prompt execution failed: %s", e)
        return {
            "response": "",
            "error": str(e),
        }
