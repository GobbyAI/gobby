"""Async MCP call dispatch for rule engine effects.

Provides ``dispatch_mcp_calls`` which executes ``mcp_call`` effects emitted
by the rule engine.  This is the async-native equivalent of
``HookManager._dispatch_mcp_calls`` (which runs in the sync hook-manager
context and needs thread-safe loop scheduling).

Used by the web-chat path (``ChatMixin._fire_lifecycle``) where we already
have a running asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextvars import Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from gobby.hooks.background_tasks import create_background_task
from gobby.hooks.events import HookEvent
from gobby.hooks.mcp_result import mcp_call_succeeded
from gobby.utils.session_context import (
    SeededContextTokens,
    SessionContext,
    reset_seeded_contexts,
    reset_session_context,
    resolve_and_seed_contexts,
    set_session_context,
)
from gobby.utils.uuid_validation import is_full_uuid

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager

# Type alias for the call_tool function signature used by MCPClientManager
CallToolFn = Callable[[str, str, dict[str, Any]], Coroutine[Any, Any, Any]]

# "explicit": the mcp_call's own arguments carried the session_id. "ambient": the
# dispatcher injected it from the event. Matches the synchronous dispatcher.
SessionRefOrigin = Literal["explicit", "ambient"]


async def dispatch_mcp_calls(
    mcp_calls: list[dict[str, Any]],
    event: HookEvent,
    call_tool_fn: CallToolFn,
    logger: logging.Logger,
    *,
    session_manager: SessionManager | None = None,
) -> list[dict[str, Any]]:
    """Dispatch ``mcp_call`` effects from a rule engine evaluation.

    Each call dict has the shape::

        {"server": "gobby-tasks", "tool": "create_task",
         "arguments": {...}, "background": True}

    Context injection:
    - ``session_id`` is injected from ``event.metadata["_platform_session_id"]``
      when not already present in the call's arguments. An empty-string
      ``session_id`` in the arguments is not replaced: the tool receives it as
      given, and nothing is resolved or seeded from it.
    - With ``session_manager``, a ``session_id`` resolves to its session UUID before
      the tool runs, and the tool receives that UUID. An explicit ref that does not
      resolve fails the call without running the tool. An ambient id that does not
      resolve runs the tool as the synchronous dispatcher does (see ``_safe_call``).
    - Without ``session_manager``, a full UUID seeds the session context as given. An
      explicit ref that is not a full UUID fails the call without running the tool;
      an ambient one runs with nothing seeded.
    - A truthy non-string ``session_id`` fails the call without running the tool, as
      the synchronous dispatcher fails it.

    Args:
        mcp_calls: List of effect dicts from ``response.metadata["mcp_calls"]``.
        event: The originating HookEvent (for context injection).
        call_tool_fn: Async callable ``(server, tool, args) -> result``,
            typically ``mcp_manager.call_tool``.
        logger: Logger for diagnostics.
        session_manager: Session store that resolves session refs. The web-chat
            path passes its tool proxy's manager.

    Returns:
        Captured results for calls using ``inject_result``,
        ``block_on_failure``, or ``block_on_success``.
    """
    dispatch_results: list[dict[str, Any]] = []
    resolver = (
        _SessionResolver(session_manager=session_manager, event_project_id=event.project_id)
        if session_manager is not None
        else None
    )
    for call in mcp_calls:
        server = call.get("server")
        tool = call.get("tool")
        arguments = dict(call.get("arguments") or {})
        # Classify before injection: only a session_id the call itself carried is explicit.
        ref_origin: SessionRefOrigin = "explicit" if "session_id" in arguments else "ambient"
        background = call.get("background", False)
        inject_result = call.get("inject_result", False)
        block_on_failure = call.get("block_on_failure", False)
        block_on_success = call.get("block_on_success", False)
        needs_capture = inject_result or block_on_failure or block_on_success

        if not server or not tool:
            logger.warning(
                "dispatch_mcp_calls: skipping call with missing server or tool: %s", call
            )
            continue

        # Inject event context into arguments.
        # Skip the call when no platform session_id could be resolved —
        # downstream lifecycle tools require a valid
        # session_id and fail with "session_id is required" when passed None.
        if "session_id" not in arguments:
            platform_sid = event.metadata.get("_platform_session_id")
            if isinstance(platform_sid, str) and platform_sid:
                arguments["session_id"] = platform_sid
            else:
                logger.warning(
                    "dispatch_mcp_calls: no platform session_id resolved for "
                    "%s/%s (event=%s, external_session_id=%s); skipping call",
                    server,
                    tool,
                    event.event_type,
                    event.session_id,
                )
                continue
        if arguments.get("prompt_text") is None:
            arguments.pop("prompt_text", None)
            event_prompt = event.data.get("prompt") if event.data else None
            if isinstance(event_prompt, str):
                arguments["prompt_text"] = event_prompt
        if "project_path" not in arguments and event.metadata.get("project_path"):
            arguments["project_path"] = event.metadata["project_path"]
        if "query" not in arguments and arguments.get("prompt_text"):
            arguments["query"] = arguments["prompt_text"]

        if needs_capture:
            try:
                result = await asyncio.wait_for(
                    _safe_call(call_tool_fn, server, tool, arguments, logger, resolver, ref_origin),
                    timeout=30.0,
                )
            except TimeoutError:
                logger.error("dispatch_mcp_calls: blocking call %s/%s timed out", server, tool)
                result = None
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
            if block_on_failure and not success:
                break
            continue

        if background:
            create_background_task(
                _safe_call(call_tool_fn, server, tool, arguments, logger, resolver, ref_origin)
            )
        else:
            try:
                await asyncio.wait_for(
                    _safe_call(call_tool_fn, server, tool, arguments, logger, resolver, ref_origin),
                    timeout=30.0,
                )
            except TimeoutError:
                logger.error("dispatch_mcp_calls: blocking call %s/%s timed out", server, tool)

    return dispatch_results


@dataclass(frozen=True)
class _SessionResolver:
    """Resolves a call's session ref through the session store before the tool runs."""

    session_manager: SessionManager
    event_project_id: str | None

    async def seed(self, ref: str, origin: SessionRefOrigin) -> SeededContextTokens:
        """Resolve ``ref`` and seed the calling task's contexts with what it resolves to.

        Only a ``#N`` or numeric ref is project-scoped, so only it takes the project
        lookup; a UUID or prefix ref passes no project. An unresolved ref seeds no
        session, and the caller decides whether that fails the call.
        """
        return await resolve_and_seed_contexts(
            ref,
            self.session_manager,
            project_ref=_project_scope(ref, self.event_project_id),
            session_ref_origin=origin,
            project_ref_is_fallback=True,
            db=self.session_manager.db,
        )


def _project_scope(ref: str, event_project_id: str | None) -> str | None:
    """Return the project a session ref is scoped to, which only ``#N`` and numeric refs are.

    An event without a project falls back to the ambient project context, as the
    pass-through did before refs were resolved.
    """
    if not ref.removeprefix("#").isdigit():
        return None
    if event_project_id:
        return event_project_id
    from gobby.utils.project_context import get_project_context

    project_ctx = get_project_context()
    ambient_id = project_ctx.get("id") if project_ctx else None
    return str(ambient_id) if ambient_id else None


async def _safe_call(
    call_tool_fn: CallToolFn,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    logger: logging.Logger,
    resolver: _SessionResolver | None,
    ref_origin: SessionRefOrigin,
) -> Any:
    """Execute a single MCP call, logging errors without propagating.

    The session_id is judged as the synchronous dispatcher judges it. A falsy value is
    absent. A truthy non-string fails the call. A string is stripped once, here, and the
    stripped text is the one that resolves and is project-scoped; a blank string is
    absent, so it is not resolved or seeded. A non-blank ref resolves before the tool
    runs, and the tool receives the resolved UUID. An explicit ref that does not resolve
    fails the call without running the tool. An ambient ref that does not resolve runs
    the tool with the id as given and seeds no session from it, as the synchronous
    dispatcher does. Without a ``resolver``, only a full UUID seeds a session.
    """
    seeded: SeededContextTokens | None = None
    session_token: Token[SessionContext | None] | None = None
    try:
        raw_ref = arguments.get("session_id")
        if raw_ref and not isinstance(raw_ref, str):
            return _refuse(logger, server, tool, f"session_id {raw_ref!r} is not a string")
        ref = raw_ref.strip() if isinstance(raw_ref, str) else ""
        if ref:
            if resolver is not None:
                seeded = await resolver.seed(ref, ref_origin)
                if seeded.resolved_session_id is not None:
                    arguments = {**arguments, "session_id": seeded.resolved_session_id}
                elif ref_origin == "explicit":
                    return _refuse(
                        logger, server, tool, f"session_id {ref!r} does not resolve to a session"
                    )
            elif is_full_uuid(ref):
                session_token = set_session_context(SessionContext(session_id=ref))
            elif ref_origin == "explicit":
                return _refuse(
                    logger,
                    server,
                    tool,
                    f"session_id {ref!r} is not a full UUID and no session store "
                    "is available to resolve it",
                )
        result = await call_tool_fn(server, tool, arguments)
        if not mcp_call_succeeded(result):
            logger.warning(
                "dispatch_mcp_calls: %s/%s returned failure: %s",
                server,
                tool,
                result.get("error", "unknown") if isinstance(result, dict) else "no result",
            )
        return result
    except Exception as exc:
        logger.exception("dispatch_mcp_calls: %s/%s failed: %s", server, tool, exc)
        return {"success": False, "error": str(exc)}
    finally:
        if seeded is not None:
            reset_seeded_contexts(seeded)
        if session_token is not None:
            reset_session_context(session_token)


def _refuse(logger: logging.Logger, server: str, tool: str, reason: str) -> dict[str, Any]:
    """Log a call that must not run and return its failure result."""
    logger.warning("dispatch_mcp_calls: %s/%s not run; %s", server, tool, reason)
    return {"success": False, "error": reason}
