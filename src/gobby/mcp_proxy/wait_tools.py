"""Helpers for MCP wrapper tools that intentionally wait."""

import asyncio
import hashlib
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context

WAIT_TOOL_NAMES = (
    "wait_for_task",
    "wait_for_any_task",
    "wait_for_all_tasks",
    "wait_for_agent",
    "wait_for_summary",
)
WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS = 15.0
WAIT_TOOL_HTTP_TIMEOUT_BUFFER_SECONDS = 30.0
MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS = 300.0
MCP_WRAPPER_EXTENDED_TOOL_TIMEOUT_SECONDS = 300.0
MCP_WRAPPER_FINGERPRINT_HEADER = "X-Gobby-MCP-Wrapper-Fingerprint"
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
    "wiki_ask",
    "wiki_compile",
)
CLIENT_GUARDED_TOOL_NAMES = (*WAIT_TOOL_NAMES, *EXTENDED_TIMEOUT_TOOL_NAMES)
HEARTBEAT_TOOL_NAMES = (*WAIT_TOOL_NAMES, *EXTENDED_TIMEOUT_TOOL_NAMES)
MCP_WRAPPER_SOURCE_PATHS = (
    Path(__file__),
    Path(__file__).with_name("stdio.py"),
)

logger = logging.getLogger("gobby.mcp.wait_tools")


@dataclass(frozen=True)
class PreparedClientGuard:
    arguments: str | dict[str, Any] | None
    timeout: float | None
    requested_timeout_seconds: float | None = None
    effective_timeout_seconds: float | None = None
    wait_timeout_capped: bool = False


def _hash_source(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _capture_source_digests(paths: tuple[Path, ...]) -> dict[str, str | None]:
    return {str(path): _hash_source(path) for path in paths}


_MCP_WRAPPER_SOURCE_DIGESTS = _capture_source_digests(MCP_WRAPPER_SOURCE_PATHS)


def _source_fingerprint(source_digests: dict[str, str | None]) -> str:
    digest = hashlib.sha256()
    for path, source_digest in sorted(source_digests.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((source_digest or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def mcp_wrapper_process_fingerprint() -> str:
    return _source_fingerprint(_MCP_WRAPPER_SOURCE_DIGESTS)


def mcp_wrapper_current_source_fingerprint() -> str:
    return _source_fingerprint(_capture_source_digests(MCP_WRAPPER_SOURCE_PATHS))


def _stale_mcp_wrapper_source_paths() -> list[str]:
    stale_paths = []
    for path, startup_digest in _MCP_WRAPPER_SOURCE_DIGESTS.items():
        if _hash_source(Path(path)) != startup_digest:
            stale_paths.append(path)
    return stale_paths


def mcp_wrapper_source_stale_result(tool_name: str) -> dict[str, Any] | None:
    if tool_name not in WAIT_TOOL_NAMES:
        return None

    stale_paths = _stale_mcp_wrapper_source_paths()
    if not stale_paths:
        return None

    return {
        "success": False,
        "error_code": MCP_WRAPPER_STALE_ERROR_CODE,
        "error": (
            "Gobby MCP stdio wrapper source changed since this MCP process started. "
            "Restart the Gobby MCP server before running wait tools."
        ),
        "tool_name": tool_name,
        "stale_source_paths": stale_paths,
        "restart_required": True,
    }


def mcp_wrapper_fingerprint_stale_result(
    tool_name: str,
    provided_fingerprint: str | None,
) -> dict[str, Any] | None:
    if tool_name not in WAIT_TOOL_NAMES:
        return None

    expected_fingerprint = mcp_wrapper_current_source_fingerprint()
    if provided_fingerprint == expected_fingerprint:
        return None

    return {
        "success": False,
        "error_code": MCP_WRAPPER_STALE_ERROR_CODE,
        "error": (
            "Gobby MCP stdio wrapper fingerprint is stale or missing. "
            "Restart the Gobby MCP server before running wait tools."
        ),
        "tool_name": tool_name,
        "provided_wrapper_fingerprint": provided_fingerprint,
        "expected_wrapper_fingerprint": expected_fingerprint,
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
        raw_timeout = None
        timeout_key = "timeout_seconds"
        if isinstance(final_args, dict):
            if "timeout" in final_args:
                timeout_key = "timeout"
                raw_timeout = final_args["timeout"]
            elif "timeout_seconds" in final_args:
                raw_timeout = final_args["timeout_seconds"]
        if raw_timeout is None:
            raw_timeout = 300.0
        try:
            requested_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            requested_timeout = None

        if (
            requested_timeout is not None
            and requested_timeout > MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS
        ):
            original_wait_timeout = requested_timeout
            requested_timeout = MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS
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
