"""MCP call routing and dispatch functions.

Extracted from HookManager — these functions handle dispatching mcp_call
effects from rule engine evaluation and formatting discovery results
for context injection.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from typing import Any

from gobby.hooks.events import HookEvent


def run_coro_blocking(
    coro: Any,
    loop: asyncio.AbstractEventLoop | None,
    logger: logging.Logger,
) -> Any:
    """Run a coroutine blocking, using the best available event loop strategy.

    Args:
        coro: The coroutine to run.
        loop: Captured event loop for thread-safe scheduling.
        logger: Logger for diagnostics.

    Returns:
        The coroutine result, or None on failure.
    """
    if loop and loop.is_running():
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=30)
        except Exception as e:
            logger.error(f"run_coro_blocking: threadsafe failed: {e}")
            return None
    else:
        try:
            return asyncio.run(coro)
        except Exception as e:
            logger.error(f"run_coro_blocking: asyncio.run failed: {e}")
            return None


async def proxy_self_call(
    proxy: Any,
    tool: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Route _proxy/* tool calls to ToolProxyService methods directly.

    This enables auto-heal rules to call list_mcp_servers, list_tools,
    and get_tool_schema without going through the MCP call_tool dispatch
    (which only routes to sub-servers, not proxy-level tools).

    Args:
        proxy: The ToolProxyService instance.
        tool: The tool name to call.
        args: Arguments for the tool call.

    Returns:
        Result dict from the proxy method.
    """
    result: dict[str, Any]
    if tool == "list_mcp_servers":
        name_filter = args.get("name_filter") if "name_filter" in args else args.get("filter")
        result = await proxy.list_servers(name_filter=name_filter)
        return result
    elif tool == "list_tools":
        server_name = args.get("server_name", "")
        result = await proxy.list_tools(server_name)
        return result
    elif tool == "get_tool_schema":
        server_name = args.get("server_name", "")
        tool_name = args.get("tool_name", "")
        result = await proxy.get_tool_schema(server_name, tool_name)
        return result
    else:
        return {"success": False, "error": f"Unknown _proxy tool: {tool}"}


def format_discovery_result(dr: dict[str, Any]) -> str:
    """Format a proxy discovery result for context injection.

    Args:
        dr: A dispatch result dict with keys: tool, result, _args, etc.

    Returns:
        Formatted string suitable for injection into agent context.
    """
    tool = dr.get("tool", "")
    result = dr.get("result") or {}

    if tool == "list_mcp_servers":
        servers = result.get("servers", [])
        lines = ["**Available MCP Servers:**"]
        for s in servers:
            lines.append(f"- {s.get('name')} ({s.get('state', 'unknown')})")
        return "\n".join(lines)

    elif tool == "list_tools":
        tools = result.get("tools", [])
        server = dr.get("_args", {}).get("server_name", result.get("server_name", ""))
        lines = [f"**Tools on {server}:**"]
        for t in tools:
            name = t.get("name", "unknown")
            brief = t.get("brief", "")
            lines.append(f"- {name}: {brief}")
        return "\n".join(lines)

    elif tool == "get_tool_schema":
        tool_info = result.get("tool", {})
        schema = tool_info.get("inputSchema", {})
        name = tool_info.get("name", "")
        desc = tool_info.get("description", "")
        return f"**Schema for {name}:**\n{desc}\n```json\n{json.dumps(schema, indent=2)}\n```"

    elif tool == "search_memories":
        memories = result.get("memories", [])
        if not memories:
            return ""
        lines = ["<project-memory>"]
        for m in memories:
            content = m.get("content", "").strip()
            if content:
                score = m.get("similarity")
                via = m.get("search_via")
                if score is not None:
                    suffix = f" (score: {score:.4f}"
                    if via:
                        suffix += f", via: {via}"
                    suffix += ")"
                else:
                    suffix = ""
                lines.append(f"- {content}{suffix}")
        lines.append("</project-memory>")
        return "\n".join(lines)

    elif tool == "search_skills":
        results = result.get("results", [])
        if not results:
            return ""
        lines = ["<available-skills>"]
        for r in results:
            name = r.get("skill_name", "unknown")
            desc = r.get("description", "")
            score = r.get("score", 0)
            if desc:
                lines.append(f"- **{name}**: {desc} (relevance: {score:.2f})")
            else:
                lines.append(f"- **{name}** (relevance: {score:.2f})")
        lines.append("")
        lines.append('Load a skill: get_skill(name="skill-name") on gobby-skills')
        lines.append(
            'Search skill hubs for more: search_hub(query="...") on gobby-skills, '
            'then install_skill(source="hub:slug") to use'
        )
        lines.append("</available-skills>")
        return "\n".join(lines)

    elif tool == "get_skill":
        skill = result.get("skill") or result.get("result", {}).get("skill") or {}
        name = skill.get("name", "unknown")
        content = skill.get("content", "")
        if content:
            return f'<skill name="{name}">\n{content}\n</skill>'
        return ""

    else:
        return f"**{tool} result:**\n```json\n{json.dumps(result, indent=2, default=str)}\n```"


def dispatch_mcp_calls(
    mcp_calls: list[dict[str, Any]],
    event: HookEvent,
    tool_proxy_getter: Any,
    loop: asyncio.AbstractEventLoop | None,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Dispatch mcp_call effects from rule engine evaluation.

    Injects event context (session_id, prompt_text) into each call's
    arguments and dispatches via ToolProxyService.  For calls with
    ``inject_result`` or ``block_on_failure``, the result is captured
    and returned so that ``_evaluate_workflow_rules`` can inject context
    or block the original tool call.

    Args:
        mcp_calls: List of mcp_call dicts from rule engine metadata.
            Each has: server, tool, arguments, background,
            inject_result, block_on_failure.
        event: The originating HookEvent (for context injection).
        tool_proxy_getter: Callable returning ToolProxyService (lazy getter).
        loop: Captured event loop for thread-safe scheduling.
        logger: Logger for diagnostics.

    Returns:
        List of result dicts for calls that had inject_result or
        block_on_failure set.  Each dict has keys: server, tool,
        inject_result, block_on_failure, success, result.
    """
    if not tool_proxy_getter:
        logger.debug("dispatch_mcp_calls: no tool_proxy_getter, skipping")
        return []

    logger.info(
        f"dispatch_mcp_calls: dispatching {len(mcp_calls)} calls for {event.event_type}",
    )

    # Capture in local so mypy narrows past the None guard for closures
    _get_proxy = tool_proxy_getter
    dispatch_results: list[dict[str, Any]] = []

    for call in mcp_calls:
        server = call.get("server")
        tool = call.get("tool")
        arguments = dict(call.get("arguments") or {})
        background = call.get("background", False)
        inject_result = call.get("inject_result", False)
        block_on_failure = call.get("block_on_failure", False)
        block_on_success = call.get("block_on_success", False)
        needs_capture = inject_result or block_on_failure or block_on_success

        if not server or not tool:
            logger.warning(f"dispatch_mcp_calls: missing server or tool in {call}")
            continue

        logger.info(f"dispatch_mcp_calls: {server}/{tool} (background={background})")

        # Inject event context into arguments
        if "session_id" not in arguments:
            arguments["session_id"] = event.metadata.get("_platform_session_id", "")
        if "prompt_text" not in arguments:
            arguments["prompt_text"] = event.data.get("prompt") if event.data else None
        if "project_path" not in arguments:
            arguments["project_path"] = event.metadata.get("project_path") or None
        # Map prompt_text to query for tools that expect it (e.g., search_memories)
        if "query" not in arguments and arguments.get("prompt_text"):
            arguments["query"] = arguments["prompt_text"]

        # Resolve session_id for context setting (needed for both project and session ContextVars)
        _event_session_id: str = arguments.get("session_id", "")

        async def _call(
            s: str,
            t: str,
            args: dict[str, Any],
            *,
            _sid: str = _event_session_id,
        ) -> dict[str, Any] | None:
            from gobby.utils.session_context import (
                reset_seeded_contexts,
                resolve_and_seed_contexts,
            )

            proxy = _get_proxy()
            session_manager = None
            if proxy and hasattr(proxy, "_mcp_manager"):
                session_manager = getattr(proxy._mcp_manager, "session_manager", None)

            tokens = resolve_and_seed_contexts(
                session_ref=_sid or None,
                session_manager=session_manager,
                project_ref=None,
                db=(session_manager.db if session_manager else None),
            )
            try:
                # Backfill project_path from ContextVar if not already set.
                # The arg injection at call-site defaults to None when event
                # metadata lacks project_path (which is always). Now that the
                # helper has populated the ContextVar, we can resolve the path.
                if not args.get("project_path"):
                    from gobby.utils.project_context import _current_project_context

                    ctx = _current_project_context.get()
                    if ctx and ctx.get("project_path"):
                        args["project_path"] = ctx["project_path"]

                if not proxy:
                    logger.warning("dispatch_mcp_calls: tool_proxy_getter returned None")
                    return {"success": False, "error": "tool_proxy_getter returned None"}

                resolved_sid = tokens.resolved_session_id or _sid or None
                # Proxy self-routing: _proxy/* calls route to ToolProxyService
                # methods directly instead of going through call_tool dispatch
                if s == "_proxy":
                    result = await proxy_self_call(proxy, t, args)
                else:
                    result = await proxy.call_tool(
                        s,
                        t,
                        args,
                        session_id=resolved_sid,
                        strip_unknown=True,
                        enforce_workflow=False,
                    )

                if isinstance(result, dict) and result.get("success") is False:
                    logger.warning(
                        f"dispatch_mcp_calls: {s}/{t} returned failure: {result.get('error', 'unknown')}",
                    )
                return result
            except Exception as exc:
                logger.error(f"dispatch_mcp_calls: {s}/{t} failed: {exc}", exc_info=True)
                return {"success": False, "error": str(exc)}
            finally:
                reset_seeded_contexts(tokens)

        # If we need to capture the result, always run blocking
        if needs_capture:
            result = run_coro_blocking(_call(server, tool, arguments), loop, logger)
            success = isinstance(result, dict) and result.get("success", False)
            dispatch_results.append(
                {
                    "server": server,
                    "tool": tool,
                    "inject_result": inject_result,
                    "block_on_failure": block_on_failure,
                    "block_on_success": block_on_success,
                    "success": success,
                    "result": result,
                }
            )
            # If this call failed and block_on_failure is set, stop processing
            if block_on_failure and not success:
                break
            continue

        coro = _call(server, tool, arguments)

        if background:
            # Fire-and-forget with error logging
            _bg_server, _bg_tool = server, tool  # bind for closure

            def _log_bg_error(
                t: asyncio.Task[Any], s: str = _bg_server, tl: str = _bg_tool
            ) -> None:
                if not t.cancelled() and t.exception():
                    logger.warning(
                        f"dispatch_mcp_calls: background {s}/{tl} failed: {t.exception()}"
                    )

            def _log_bg_future_error(
                f: concurrent.futures.Future[Any],
                s: str = _bg_server,
                tl: str = _bg_tool,
            ) -> None:
                if not f.cancelled():
                    exc: BaseException | None = f.exception()
                    if exc is not None:
                        logger.warning(f"dispatch_mcp_calls: background {s}/{tl} failed: {exc}")

            try:
                running_loop = asyncio.get_running_loop()
                task = running_loop.create_task(coro)
                task.add_done_callback(_log_bg_error)
            except RuntimeError:
                if loop and loop.is_running():
                    try:
                        future = asyncio.run_coroutine_threadsafe(coro, loop)
                        future.add_done_callback(_log_bg_future_error)
                    except Exception as e:
                        logger.warning(
                            f"dispatch_mcp_calls: failed to schedule {server}/{tool}: {e}",
                        )
                else:
                    try:
                        asyncio.run(coro)
                    except Exception as e:
                        logger.warning(
                            f"dispatch_mcp_calls: background {server}/{tool} failed: {e}",
                        )
        else:
            # Blocking dispatch -- must await completion, not fire-and-forget
            run_coro_blocking(coro, loop, logger)

    return dispatch_results
