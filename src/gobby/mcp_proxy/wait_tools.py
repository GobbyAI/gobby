"""Helpers for MCP wrapper tools that intentionally wait."""

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context

WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS = 15.0
WAIT_TOOL_HTTP_TIMEOUT_BUFFER_SECONDS = 30.0
MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS = 300.0
WAIT_TOOL_NAMES = ("wait_for_output", "wait_for_summary")
MCP_WRAPPER_EXTENDED_TOOL_TIMEOUT_SECONDS = 300.0
MCP_WRAPPER_PROTOCOL_VERSION = "1"
MCP_WRAPPER_PROTOCOL_VERSION_HEADER = "X-Gobby-MCP-Wrapper-Protocol-Version"
MCP_WRAPPER_STALE_ERROR_CODE = "GOBBY_MCP_WRAPPER_STALE"
WAIT_TOOL_WRAPPER_GRACE_SECONDS = 5.0
EXTENDED_TIMEOUT_TOOL_NAMES = (
    "close_task",
    "expand_task",
    "merge_resolve",
    "suggest_next_task",
    "compact_self",
    "recall_review_context",
    "rebuild_knowledge_graph",
    # Worktree merges allow a 60s git subprocess and perform additional git,
    # storage, and cleanup work. Keep the HTTP caller alive for the bounded
    # daemon operation so its authoritative result is not lost (#17900).
    "merge_worktree",
    "sync_worktree",
    # Generation-backed gwiki calls: daemon-side synthesis scales with vault
    # size and cannot fit the default 30s request timeout (#17593). The
    # daemon's gwiki subprocess guard (GENERATION_GWIKI_TIMEOUT_SECONDS) sits
    # 30s below this HTTP cap so structured timeout envelopes still arrive.
    "wiki_compile",
    # Agent spawn performs bounded isolation repair (env preseed, hook copy),
    # child-session creation, and tmux launch; under concurrent fleet load the
    # daemon side exceeds 30s while still succeeding, so keep the caller alive
    # for the authoritative run record instead of losing the result envelope.
    "spawn_agent",
    # Plan-coverage QA is deliberately synchronous blocking work whose duration
    # scales with the expanded task tree, so it exceeds 30s on real trees while
    # completing server-side and persisting its manifest. Keep the caller alive
    # for the authoritative QA verdict instead of a REQUEST_TIMEOUT that hides a
    # successful run (#19095).
    "run_expansion_qa_coverage",
)


def clamp_wait_tool_timeout(
    tool_name: str,
    timeout_seconds: float | int | str,
    *,
    default: float,
) -> float:
    """Clamp a wait tool's requested timeout to its wrapper-level ceiling."""
    try:
        requested_timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        requested_timeout = default
    return max(0.0, min(requested_timeout, wait_tool_timeout_limit(tool_name)))


def wait_tool_timeout_limit(tool_name: str) -> float:
    """Return the shared wrapper ceiling for a registered wait tool."""
    if tool_name not in WAIT_TOOL_NAMES:
        raise KeyError(tool_name)
    return MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS


CLIENT_GUARDED_TOOL_NAMES = (*WAIT_TOOL_NAMES, *EXTENDED_TIMEOUT_TOOL_NAMES)
HEARTBEAT_TOOL_NAMES = (*WAIT_TOOL_NAMES, *EXTENDED_TIMEOUT_TOOL_NAMES)

logger = logging.getLogger("gobby.mcp.wait_tools")


@dataclass(frozen=True)
class PreparedClientGuard:
    arguments: str | dict[str, Any] | None
    timeout: float | None
    requested_timeout_seconds: float | None = None
    effective_timeout_seconds: float | None = None
    wait_timeout_capped: bool = False


def mcp_wrapper_protocol_mismatch_result(
    tool_name: str,
    provided_protocol_version: str | None,
) -> dict[str, Any] | None:
    if tool_name not in WAIT_TOOL_NAMES:
        return None

    if provided_protocol_version == MCP_WRAPPER_PROTOCOL_VERSION:
        return None

    return {
        "success": False,
        "error_code": MCP_WRAPPER_STALE_ERROR_CODE,
        "error": (
            "Gobby MCP stdio wrapper protocol version is missing or incompatible. "
            "Restart the MCP client session before running wait tools."
        ),
        "tool_name": tool_name,
        "provided_wrapper_protocol_version": provided_protocol_version,
        "expected_wrapper_protocol_version": MCP_WRAPPER_PROTOCOL_VERSION,
        "restart_required": True,
    }


def prepare_client_guard(
    *,
    tool_name: str,
    arguments: str | dict[str, Any] | None,
) -> PreparedClientGuard:
    requested_timeout = None
    original_wait_timeout = None
    wait_timeout_capped = False
    final_args = arguments

    if tool_name in WAIT_TOOL_NAMES:
        timeout_limit = wait_tool_timeout_limit(tool_name)
        raw_timeout = None
        timeout_key = "timeout_seconds"
        if isinstance(final_args, dict):
            if "timeout" in final_args:
                timeout_key = "timeout"
                raw_timeout = final_args["timeout"]
            elif "timeout_seconds" in final_args:
                raw_timeout = final_args["timeout_seconds"]
        if raw_timeout is None:
            raw_timeout = timeout_limit
        try:
            requested_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            requested_timeout = None

        if requested_timeout is not None and requested_timeout > timeout_limit:
            original_wait_timeout = requested_timeout
            requested_timeout = timeout_limit
            final_args = dict(final_args) if isinstance(final_args, dict) else {}
            final_args[timeout_key] = requested_timeout
            wait_timeout_capped = True
    elif tool_name in EXTENDED_TIMEOUT_TOOL_NAMES:
        requested_timeout = MCP_WRAPPER_EXTENDED_TOOL_TIMEOUT_SECONDS

    return PreparedClientGuard(
        arguments=final_args,
        timeout=requested_timeout,
        requested_timeout_seconds=original_wait_timeout,
        effective_timeout_seconds=requested_timeout if wait_timeout_capped else None,
        wait_timeout_capped=wait_timeout_capped,
    )


def _wrapper_timeout_result(tool_name: str, timeout: float) -> dict[str, Any]:
    return {
        "success": True,
        "completed": False,
        "timeout_seconds": timeout,
        "effective_timeout_seconds": timeout,
        "mcp_wrapper_timeout": True,
        "background_call_continues": True,
        "tool_name": tool_name,
    }


def _consume_background_result(task: asyncio.Task[dict[str, Any]]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("Background MCP wrapper call failed after timeout: %s", exc)


async def _await_with_guard(
    tool_call: Awaitable[dict[str, Any]],
    *,
    tool_name: str,
    timeout: float | None,
) -> dict[str, Any]:
    if tool_name not in CLIENT_GUARDED_TOOL_NAMES or timeout is None:
        return await tool_call
    task = asyncio.ensure_future(tool_call)
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout + WAIT_TOOL_WRAPPER_GRACE_SECONDS,
        )
    except TimeoutError:
        task.add_done_callback(_consume_background_result)
        return _wrapper_timeout_result(tool_name, timeout)


async def call_with_wait_heartbeat(
    tool_call: Awaitable[dict[str, Any]],
    *,
    ctx: Context[Any, Any, Any] | None,
    tool_name: str,
    timeout: float | None,
) -> dict[str, Any]:
    """Keep stdio transport active while a wait-capable proxied tool blocks."""
    if ctx is None or tool_name not in HEARTBEAT_TOOL_NAMES:
        return await _await_with_guard(tool_call, tool_name=tool_name, timeout=timeout)

    stop_event = asyncio.Event()

    async def _heartbeat() -> None:
        elapsed = 0.0
        while True:
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS,
                )
                return
            except TimeoutError:
                elapsed += WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS
                progress = min(elapsed, timeout) if timeout is not None else elapsed
                try:
                    await ctx.report_progress(
                        progress=progress,
                        total=timeout,
                        message=f"{tool_name} still waiting for daemon result",
                    )
                except Exception:
                    logger.warning(
                        "Failed to report %s wait heartbeat",
                        tool_name,
                        exc_info=True,
                    )

    heartbeat_task = asyncio.create_task(_heartbeat(), name=f"{tool_name}-heartbeat")
    try:
        return await _await_with_guard(tool_call, tool_name=tool_name, timeout=timeout)
    finally:
        stop_event.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning(
                "%s wait heartbeat task failed during cleanup",
                tool_name,
                exc_info=True,
            )
