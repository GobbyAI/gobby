"""Step execution handlers for pipeline workflows."""

import asyncio
import logging
import shlex
from typing import Any

logger = logging.getLogger(__name__)


async def execute_mcp_step(
    rendered_step: Any, context: dict[str, Any], tool_proxy_getter: Any | None
) -> Any:
    """Execute an MCP tool call step."""
    mcp_config = rendered_step.mcp

    logger.info(f"Executing MCP step: {mcp_config.server}:{mcp_config.tool}")

    if not tool_proxy_getter:
        raise RuntimeError(
            f"MCP step {rendered_step.id} requires tool_proxy_getter but none configured"
        )

    tool_proxy = tool_proxy_getter()
    if not tool_proxy:
        raise RuntimeError("tool_proxy_getter returned None")

    result = await tool_proxy.call_tool(
        mcp_config.server,
        mcp_config.tool,
        mcp_config.arguments or {},
    )

    # Convert MCP SDK CallToolResult to a serializable dict
    if hasattr(result, "content") and hasattr(result, "isError"):
        texts = []
        for item in result.content:
            if hasattr(item, "text"):
                texts.append(item.text)
        output = "\n".join(texts) if texts else ""
        if getattr(result, "isError", False):
            raise RuntimeError(
                f"MCP step {rendered_step.id} failed: "
                f"{mcp_config.server}:{mcp_config.tool} returned error: {output}"
            )
        return {"result": output}

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
    logger.info(f"Executing command: {command}")

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
        logger.error(f"Command execution failed: {e}", exc_info=True)
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": 1,
        }


async def execute_prompt_step(
    prompt: str, context: dict[str, Any], llm_service: Any
) -> dict[str, Any]:
    """Execute an LLM prompt step."""
    if not llm_service:
        return {"error": "prompt step requires llm_service but none configured"}

    try:
        provider = llm_service.get_default_provider()
        response = await provider.generate_text(prompt)
        return {"response": response}
    except (OSError, RuntimeError, ValueError) as e:
        logger.error(f"LLM prompt execution failed: {e}", exc_info=True)
        return {
            "response": "",
            "error": str(e),
        }
