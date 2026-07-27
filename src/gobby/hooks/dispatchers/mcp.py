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
from dataclasses import dataclass, field
from typing import Any, Literal

from gobby.hooks.background_tasks import create_background_task
from gobby.hooks.effect_deadline import remaining_blocking_effect_seconds
from gobby.hooks.events import HookEvent
from gobby.hooks.mcp_result import mcp_call_succeeded
from gobby.llm.sdk_utils import ADDITIONAL_CONTEXT_LIMIT
from gobby.mcp_proxy.server_list import compact_mcp_server_list
from gobby.memory.context import format_memory_metadata_suffix
from gobby.review_learning.guidance import format_review_lesson_guidance
from gobby.skills.formatting import skill_fetch_directive

REVIEW_LESSON_TAG = "review-lesson"
# Project memories share the SDK additional-context budget; cap them at 4k chars
# and never allow them to consume more than half of the total context.
PROJECT_MEMORY_CONTEXT_BUDGET = min(4_000, ADDITIONAL_CONTEXT_LIMIT // 2)
PROJECT_MEMORY_OPEN_TAG = "<project-memory>"
PROJECT_MEMORY_CLOSE_TAG = "</project-memory>"


def run_coro_blocking(
    coro: Any,
    loop: asyncio.AbstractEventLoop | None,
    logger: logging.Logger,
    *,
    label: str | None = None,
    timeout_seconds: float = 30,
) -> Any:
    """Run a coroutine blocking, using the best available event loop strategy.

    Args:
        coro: The coroutine to run.
        loop: Captured event loop for thread-safe scheduling.
        logger: Logger for diagnostics.
        label: Optional call label for diagnostics.
        timeout_seconds: Timeout for thread-safe scheduling.

    Returns:
        The coroutine result, or None on failure.
    """
    label_suffix = f"[{label}]" if label else ""
    if loop and loop.is_running():
        future: concurrent.futures.Future[Any] | None = None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as e:
            if future is not None:
                future.cancel()
            logger.exception(
                "run_coro_blocking%s: threadsafe failed after %ss: %s: %s",
                label_suffix,
                timeout_seconds,
                type(e).__name__,
                e,
            )
            return None
        except Exception as e:
            logger.exception(
                "run_coro_blocking%s: threadsafe failed: %s: %s",
                label_suffix,
                type(e).__name__,
                e,
            )
            return None
    else:
        try:

            async def run_with_timeout() -> Any:
                return await asyncio.wait_for(coro, timeout=timeout_seconds)

            return asyncio.run(run_with_timeout())
        except Exception as e:
            logger.exception(
                "run_coro_blocking%s: asyncio.run failed: %s: %s",
                label_suffix,
                type(e).__name__,
                e,
            )
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
        return compact_mcp_server_list(result)
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


def _format_hub_auth_status(hub: dict[str, Any]) -> str:
    """Return compact auth status text for a skill hub."""
    auth_required = hub.get("auth_required")
    auth_configured = hub.get("auth_configured")
    if auth_required is True:
        if auth_configured is True:
            return "configured"
        key_name = hub.get("auth_key_name")
        return f"missing {key_name}" if key_name else "required"
    if auth_required is False:
        return "not required"
    return "unknown"


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
            if isinstance(s, str):
                lines.append(f"- {s}")
            elif isinstance(s, dict):
                lines.append(f"- {s.get('name')} ({s.get('state', 'unknown')})")
        issues = result.get("issues") or []
        if issues:
            lines.append("")
            lines.append("**MCP Server Issues:**")
            for issue in issues:
                if isinstance(issue, dict):
                    lines.append(f"- {issue.get('name')} ({issue.get('state', 'unknown')})")
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
        memories = [m for m in memories if not _is_review_lesson_memory(m)]
        return _format_project_memories(memories)

    elif tool == "recall_review_lessons_for_files":
        lessons = result.get("lessons", [])
        if not lessons:
            return ""
        return format_review_lesson_guidance(lessons)

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

    elif tool == "list_hubs":
        hubs = result.get("hubs", [])
        lines = ["<available-skill-hubs>"]
        for hub in hubs:
            name = hub.get("name", "unknown")
            hub_type = hub.get("type", "unknown")
            auth = _format_hub_auth_status(hub)
            lines.append(f"- {name} ({hub_type}, auth: {auth})")
        if not hubs:
            lines.append("- none configured")
        lines.append("")
        lines.append('Search hubs: search_hub(query="...", hub_name="optional") on gobby-skills')
        lines.append("</available-skill-hubs>")
        return "\n".join(lines)

    elif tool == "get_skill":
        skill = result.get("skill") or result.get("result", {}).get("skill") or {}
        name = skill.get("name", "unknown")
        return skill_fetch_directive(name) if name != "unknown" else ""

    else:
        return f"**{tool} result:**\n```json\n{json.dumps(result, indent=2, default=str)}\n```"


def _is_review_lesson_memory(memory: Any) -> bool:
    """Return True when ``memory`` is a review lesson tagged with ``REVIEW_LESSON_TAG``.

    Expects a dict with a ``tags`` iterable; non-dicts or non-iterable tag values
    return False.
    """
    if not isinstance(memory, dict):
        return False
    tags = memory.get("tags")
    if not isinstance(tags, (list, tuple, set, frozenset)):
        return False
    return REVIEW_LESSON_TAG in tags


def _project_memory_omitted_line(count: int) -> str:
    noun = "memory" if count == 1 else "memories"
    return f"- ... {count} lower-ranked {noun} omitted due to context budget."


def _render_project_memory(body_lines: list[str], omitted_count: int = 0) -> str:
    if not body_lines:
        return ""
    lines = [PROJECT_MEMORY_OPEN_TAG, *body_lines]
    if omitted_count:
        lines.append(_project_memory_omitted_line(omitted_count))
    lines.append(PROJECT_MEMORY_CLOSE_TAG)
    return "\n".join(lines)


def _project_memory_render_len(body_lines: list[str], omitted_count: int = 0) -> int:
    return len(_render_project_memory(body_lines, omitted_count))


def _project_memory_next_line_budget(
    body_lines: list[str],
    omitted_count: int,
    budget: int,
) -> int:
    # Reserve room for the trailing blank separator included by _render_project_memory().
    return budget - _project_memory_render_len(body_lines + [""], omitted_count)


def _fit_memory_line(content: str, suffix: str, max_len: int) -> str | None:
    prefix = "- "
    marker = "..."
    minimum_len = len(prefix) + len(marker) + len(suffix)
    if max_len < minimum_len:
        return None

    full_line = f"{prefix}{content}{suffix}"
    if len(full_line) <= max_len:
        return full_line

    content_budget = max_len - len(prefix) - len(marker) - len(suffix)
    truncated = content[:content_budget].rstrip()
    return f"{prefix}{truncated}{marker}{suffix}"


@dataclass
class ProjectMemoryRenderOutcome:
    """Per-memory render decisions for the ``<project-memory>`` block.

    Feeds the durable injection-outcome record (contract §5): ``rendered_ids``
    are injected in render order (index = ``injection_position``);
    ``empty_content_ids`` and ``omitted_ids`` map to the ``empty_content`` and
    ``budget`` drop reasons. Memories without an ``id`` are untracked.
    """

    rendered_ids: list[str] = field(default_factory=list)
    empty_content_ids: list[str] = field(default_factory=list)
    omitted_ids: list[str] = field(default_factory=list)


def format_project_memories_with_outcome(
    memories: list[Any],
    *,
    budget: int = PROJECT_MEMORY_CONTEXT_BUDGET,
) -> tuple[str, ProjectMemoryRenderOutcome]:
    outcome = ProjectMemoryRenderOutcome()
    candidates: list[tuple[str | None, str, str]] = []
    for memory in memories:
        if not isinstance(memory, dict):
            continue
        memory_id = memory.get("id")
        if memory_id is not None:
            memory_id = str(memory_id)
        content = str(memory.get("content", "")).strip()
        if not content:
            if memory_id:
                outcome.empty_content_ids.append(memory_id)
            continue

        score = memory.get("similarity")
        via = memory.get("search_via")
        suffix = format_memory_metadata_suffix(memory_id, score=score, via=via)
        candidates.append((memory_id, content, suffix))

    if not candidates:
        return "", outcome

    body_lines: list[str] = []
    omitted_count = 0
    for index, (_, content, suffix) in enumerate(candidates):
        remaining_after_current = len(candidates) - index - 1
        full_line = f"- {content}{suffix}"
        if _project_memory_render_len(body_lines + [full_line], remaining_after_current) <= budget:
            body_lines.append(full_line)
            continue

        max_line_len = _project_memory_next_line_budget(
            body_lines,
            remaining_after_current,
            budget,
        )
        truncated_line = _fit_memory_line(content, suffix, max_line_len)
        if truncated_line:
            body_lines.append(truncated_line)
            omitted_count = remaining_after_current
        else:
            omitted_count = len(candidates) - index
        break

    result = _render_project_memory(body_lines, omitted_count)
    while body_lines and len(result) > budget:
        body_lines.pop()
        omitted_count += 1
        result = _render_project_memory(body_lines, omitted_count)
    if len(result) > budget:
        result = ""
        body_lines = []
    # The render loop appends at most one line per candidate, in order, so the
    # first len(body_lines) candidates are exactly the rendered ones.
    rendered_n = len(body_lines)
    outcome.rendered_ids = [mid for mid, _, _ in candidates[:rendered_n] if mid]
    outcome.omitted_ids = [mid for mid, _, _ in candidates[rendered_n:] if mid]
    return result, outcome


def _format_project_memories(
    memories: list[Any],
    *,
    budget: int = PROJECT_MEMORY_CONTEXT_BUDGET,
) -> str:
    text, _ = format_project_memories_with_outcome(memories, budget=budget)
    return text


def dispatch_mcp_calls(
    mcp_calls: list[dict[str, Any]],
    event: HookEvent,
    tool_proxy_getter: Any,
    loop: asyncio.AbstractEventLoop | None,
    logger: logging.Logger,
    *,
    deadline: float | None = None,
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

    logger.debug(
        "dispatch_mcp_calls: dispatching %s calls for %s", len(mcp_calls), event.event_type
    )

    # Capture in local so mypy narrows past the None guard for closures
    _get_proxy = tool_proxy_getter
    dispatch_results: list[dict[str, Any]] = []

    for call in mcp_calls:
        server = call.get("server")
        tool = call.get("tool")
        arguments = dict(call.get("arguments") or {})
        session_id_is_explicit = "session_id" in arguments
        background = call.get("background", False)
        inject_result = call.get("inject_result", False)
        block_on_failure = call.get("block_on_failure", False)
        block_on_success = call.get("block_on_success", False)
        needs_capture = inject_result or block_on_failure or block_on_success

        if not server or not tool:
            logger.warning(
                "dispatch_mcp_calls: missing server or tool",
                extra={"server": call.get("server"), "tool": call.get("tool")},
            )
            continue

        logger.debug("dispatch_mcp_calls: %s/%s (background=%s)", server, tool, background)

        # Inject event context into arguments.
        # ``_platform_session_id`` may be present with value None when
        # SessionLookupService.resolve() could not map the external id; coerce
        # to "" so downstream tools receive a str, not None.
        if "session_id" not in arguments:
            arguments["session_id"] = event.metadata.get("_platform_session_id") or ""
        if arguments.get("prompt_text") is None:
            arguments.pop("prompt_text", None)
            event_prompt = event.data.get("prompt") if event.data else None
            if isinstance(event_prompt, str):
                arguments["prompt_text"] = event_prompt
        if "project_path" not in arguments:
            arguments["project_path"] = event.metadata.get("project_path") or None
        # Map prompt_text to query for tools that expect it (e.g., search_memories)
        if "query" not in arguments and arguments.get("prompt_text"):
            arguments["query"] = arguments["prompt_text"]

        # Resolve session_id for context setting (needed for both project and session ContextVars)
        _event_session_id: str = arguments.get("session_id", "")
        _session_ref_origin: Literal["explicit", "ambient"] = (
            "explicit" if session_id_is_explicit else "ambient"
        )
        _event_project_id = event.project_id

        async def _call(
            s: str,
            t: str,
            args: dict[str, Any],
            *,
            _sid: str = _event_session_id,
            _project_id: str | None = _event_project_id,
            _origin: Literal["explicit", "ambient"] = _session_ref_origin,
        ) -> Any:
            from gobby.utils.session_context import (
                reset_seeded_contexts,
                resolve_and_seed_contexts,
            )

            proxy = _get_proxy()
            session_manager = proxy.session_manager if proxy else None

            tokens = await resolve_and_seed_contexts(
                session_ref=_sid or None,
                session_manager=session_manager,
                project_ref=_project_id,
                session_ref_origin=_origin,
                project_ref_is_fallback=True,
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

                resolved_sid = tokens.resolved_session_id
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

                if not mcp_call_succeeded(result):
                    logger.warning(
                        "dispatch_mcp_calls: %s/%s returned failure: %s",
                        s,
                        t,
                        result.get("error", "unknown") if isinstance(result, dict) else "no result",
                    )
                return result
            except Exception as exc:
                logger.exception(
                    "dispatch_mcp_calls: %s/%s failed: %s: %s",
                    s,
                    t,
                    type(exc).__name__,
                    exc,
                )
                return {"success": False, "error": str(exc)}
            finally:
                reset_seeded_contexts(tokens)

        # If we need to capture the result, always run blocking
        if needs_capture:
            event_type_label = getattr(event.event_type, "value", event.event_type)
            label = f"{event_type_label}:{server}/{tool}"
            timeout_seconds = remaining_blocking_effect_seconds(deadline, maximum=30.0)
            if timeout_seconds <= 0:
                logger.error("dispatch_mcp_calls[%s]: aggregate blocking deadline exceeded", label)
                result = None
            else:
                result = run_coro_blocking(
                    _call(server, tool, arguments),
                    loop,
                    logger,
                    label=label,
                    timeout_seconds=timeout_seconds,
                )
            success = mcp_call_succeeded(result)
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
                        "dispatch_mcp_calls: background %s/%s failed: %s", s, tl, t.exception()
                    )

            def _log_bg_future_error(
                f: concurrent.futures.Future[Any],
                s: str = _bg_server,
                tl: str = _bg_tool,
            ) -> None:
                if not f.cancelled():
                    exc: BaseException | None = f.exception()
                    if exc is not None:
                        logger.warning(
                            "dispatch_mcp_calls: background %s/%s failed: %s", s, tl, exc
                        )

            try:
                running_loop = asyncio.get_running_loop()
                task = create_background_task(coro, loop=running_loop)
                task.add_done_callback(_log_bg_error)
            except RuntimeError:
                if loop and loop.is_running():
                    try:
                        future = asyncio.run_coroutine_threadsafe(coro, loop)
                        future.add_done_callback(_log_bg_future_error)
                    except Exception as e:
                        logger.warning(
                            "dispatch_mcp_calls: failed to schedule %s/%s: %s", server, tool, e
                        )
                else:
                    try:
                        asyncio.run(coro)
                    except Exception as e:
                        logger.warning(
                            "dispatch_mcp_calls: background %s/%s failed: %s", server, tool, e
                        )
        else:
            # Blocking dispatch -- must await completion, not fire-and-forget
            event_type_label = getattr(event.event_type, "value", event.event_type)
            label = f"{event_type_label}:{server}/{tool}"
            timeout_seconds = remaining_blocking_effect_seconds(deadline, maximum=30.0)
            if timeout_seconds <= 0:
                coro.close()
                logger.error("dispatch_mcp_calls[%s]: aggregate blocking deadline exceeded", label)
            else:
                run_coro_blocking(
                    coro,
                    loop,
                    logger,
                    label=label,
                    timeout_seconds=timeout_seconds,
                )

    return dispatch_results
