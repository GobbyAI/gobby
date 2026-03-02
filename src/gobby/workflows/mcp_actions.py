"""MCP tool invocation workflow actions.

Extracted from actions.py as part of strangler fig decomposition.
These functions handle MCP tool calls from workflows.
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.workflows.templates import TemplateEngine

logger = logging.getLogger(__name__)


def _has_template_syntax(value: Any) -> bool:
    """Recursively check if a value contains Jinja2 template syntax."""
    if isinstance(value, str):
        return "{{" in value
    if isinstance(value, dict):
        return any(_has_template_syntax(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_template_syntax(item) for item in value)
    return False


def _render_value(
    value: Any,
    template_engine: "TemplateEngine",
    template_context: dict[str, Any],
) -> Any:
    """Recursively render a single value through the template engine."""
    if isinstance(value, str) and "{{" in value:
        return template_engine.render(value, template_context)
    elif isinstance(value, dict):
        return _render_arguments(value, template_engine, template_context)
    elif isinstance(value, list):
        return [_render_value(item, template_engine, template_context) for item in value]
    return value


def _render_arguments(
    arguments: dict[str, Any],
    template_engine: "TemplateEngine",
    template_context: dict[str, Any],
) -> dict[str, Any]:
    """Recursively render Jinja2 templates in argument values.

    Args:
        arguments: Dict of arguments, values may contain {{ templates }}
        template_engine: Jinja2 template engine
        template_context: Context for template rendering

    Returns:
        New dict with all string values rendered through the template engine
    """
    return {
        key: _render_value(value, template_engine, template_context)
        for key, value in arguments.items()
    }


async def call_mcp_tool(
    tool_proxy_getter: Any,
    state: Any,
    server_name: str | None,
    tool_name: str | None,
    arguments: dict[str, Any] | None = None,
    output_as: str | None = None,
) -> dict[str, Any]:
    """Call an MCP tool via ToolProxyService (handles both internal and external servers).

    Args:
        tool_proxy_getter: Callable returning ToolProxyService instance
        state: WorkflowState object for storing results
        server_name: Name of the MCP server
        tool_name: Name of the tool to call
        arguments: Arguments to pass to the tool
        output_as: Optional variable name to store result

    Returns:
        Dict with result and stored_as, or error
    """
    if not server_name or not tool_name:
        return {"error": "Missing server_name or tool_name"}

    if not tool_proxy_getter:
        logger.warning("call_mcp_tool: tool_proxy_getter not available")
        return {"error": "Tool proxy not available"}

    try:
        tool_proxy = tool_proxy_getter()
        if not tool_proxy:
            logger.warning("call_mcp_tool: tool_proxy_getter returned None")
            return {"error": "Tool proxy not available"}

        # Call tool via ToolProxyService (routes to internal or external servers)
        result = await tool_proxy.call_tool(server_name, tool_name, arguments or {})

        # Store result in workflow variable if 'as' specified
        if output_as:
            if state is None:
                raise ValueError("state must be provided when output_as is specified")
            if not state.variables:
                state.variables = {}
            state.variables[output_as] = result

        return {"result": result, "stored_as": output_as}
    except Exception as e:
        logger.error(f"call_mcp_tool: Failed: {e}")
        return {"error": str(e)}


# --- ActionHandler-compatible wrappers ---
# These match the ActionHandler protocol: (context: ActionContext, **kwargs) -> dict | None


async def handle_call_mcp_tool(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for call_mcp_tool.

    Supports template rendering in server_name, tool_name, and argument values.
    Accepts both 'as' and 'output_as' kwargs for storing the result in a variable.
    Returns inject_message with a summary of the call for LLM context.
    """
    template_engine = context.template_engine
    template_context = {
        "variables": context.state.variables or {},
        "state": context.state,
        "session_id": context.session_id,
    }

    # Resolve output_as from either 'as' or 'output_as' kwarg
    output_as = kwargs.get("as") or kwargs.get("output_as")

    # Render template strings in server_name and tool_name
    server_name = kwargs.get("server_name") or ""
    tool_name = kwargs.get("tool_name") or ""

    if template_engine:
        if "{{" in server_name:
            server_name = template_engine.render(server_name, template_context)
        if "{{" in tool_name:
            tool_name = template_engine.render(tool_name, template_context)
    else:
        has_templates = "{{" in server_name or "{{" in tool_name
        if not has_templates:
            has_templates = _has_template_syntax(kwargs.get("arguments") or {})
        if has_templates:
            logger.warning(
                "handle_call_mcp_tool: template syntax detected but no template_engine configured; "
                "templates will not be rendered"
            )

    # Render template strings in arguments
    arguments = kwargs.get("arguments") or {}
    if template_engine and arguments:
        arguments = _render_arguments(arguments, template_engine, template_context)

    result = await call_mcp_tool(
        tool_proxy_getter=context.tool_proxy_getter,
        state=context.state,
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
        output_as=output_as,
    )

    # Return inject_message so the LLM sees what was auto-executed
    if "error" not in result:
        summary = f"[Auto-executed] {server_name}/{tool_name}"
        if output_as:
            summary += f" → stored as {output_as}"
        result["inject_message"] = summary

    return result
