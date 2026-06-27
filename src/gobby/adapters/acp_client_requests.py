"""Client-directed ACP JSON-RPC request handling."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from gobby.adapters.acp_client import StreamEvent, _make_id
from gobby.adapters.acp_filesystem import (
    ACPFileSystemError,
    read_text_file,
    roots_for_client,
    write_text_file,
)
from gobby.adapters.acp_terminal import TerminalNotFoundError

logger = logging.getLogger(__name__)
PreToolCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
_PRE_TOOL_DENY_DECISIONS = {"deny", "block", "decline", "reject"}


async def write_json_rpc_result(client: Any, request_id: Any, result: Any) -> None:
    process = getattr(client, "_process", None)
    if not process or not process.stdin:
        return
    response = {"jsonrpc": "2.0", "id": request_id, "result": result}
    await _write_json_rpc_response(process, response)


async def write_json_rpc_error(client: Any, request_id: Any, *, code: int, message: str) -> None:
    process = getattr(client, "_process", None)
    if not process or not process.stdin:
        return
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    await _write_json_rpc_response(process, response)


async def _write_json_rpc_response(process: Any, response: dict[str, Any]) -> None:
    try:
        process.stdin.write((json.dumps(response) + "\n").encode())
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        logger.debug("ACP client pipe closed while writing JSON-RPC response", exc_info=True)


async def handle_client_request(
    client: Any,
    request: dict[str, Any],
    *,
    pre_tool_callback: PreToolCallback | None = None,
) -> AsyncIterator[StreamEvent]:
    method = request.get("method")
    if method in {"fs/read_text_file", "fs/write_text_file"}:
        async for event in _handle_filesystem_request(
            client, request, pre_tool_callback=pre_tool_callback
        ):
            yield event
        return

    if method in {
        "terminal/create",
        "terminal/output",
        "terminal/wait_for_exit",
        "terminal/kill",
        "terminal/release",
    }:
        async for event in _handle_terminal_request(
            client, request, pre_tool_callback=pre_tool_callback
        ):
            yield event
        return

    if method == "session/request_permission":
        await _handle_request_permission_request(
            client, request, pre_tool_callback=pre_tool_callback
        )
        return

    await write_json_rpc_error(
        client,
        request.get("id"),
        code=-32601,
        message=f"Unknown client request method: {method}",
    )


async def _handle_filesystem_request(
    client: Any,
    request: dict[str, Any],
    *,
    pre_tool_callback: PreToolCallback | None = None,
) -> AsyncIterator[StreamEvent]:
    method = str(request.get("method") or "")
    params = request.get("params")
    if not isinstance(params, dict):
        await write_json_rpc_error(
            client,
            request.get("id"),
            code=-32602,
            message=f"{method} params must be an object",
        )
        return

    session_error = _validate_session_id(client, params)
    if session_error is not None:
        await write_json_rpc_error(client, request.get("id"), code=-32602, message=session_error)
        return

    roots = roots_for_client(client)
    try:
        if method == "fs/read_text_file":
            content = await asyncio.to_thread(
                read_text_file,
                params.get("path"),
                roots,
                line=params.get("line"),
                limit=params.get("limit"),
            )
            await write_json_rpc_result(client, request.get("id"), {"content": content})
            return
        if method == "fs/write_text_file":
            async for event in _handle_write_text_file(
                client,
                request,
                params,
                roots,
                pre_tool_callback=pre_tool_callback,
            ):
                yield event
            return
    except ACPFileSystemError as exc:
        await write_json_rpc_error(client, request.get("id"), code=exc.code, message=str(exc))
        return

    await write_json_rpc_error(
        client,
        request.get("id"),
        code=-32601,
        message=f"Unknown filesystem method: {method}",
    )


async def _handle_write_text_file(
    client: Any,
    request: dict[str, Any],
    params: dict[str, Any],
    roots: Iterable[Path],
    *,
    pre_tool_callback: PreToolCallback | None = None,
) -> AsyncIterator[StreamEvent]:
    path = params.get("path")
    tool_input = {"path": path, "content": params.get("content")}
    pre_tool_response = await _apply_pre_tool_decision(
        pre_tool_callback,
        tool_name="write_text_file",
        tool_input=tool_input,
    )
    if is_pre_tool_decision_denied(pre_tool_response):
        await write_json_rpc_error(
            client,
            request.get("id"),
            code=-32000,
            message=pre_tool_denial_reason(pre_tool_response),
        )
        return

    call_id = str(request.get("id") or f"fs-write-{_make_id()}")
    yield StreamEvent(
        event_type="tool_call",
        data={
            "call_id": call_id,
            "tool_name": "write_text_file",
            "tool_input": tool_input,
            "mcp_server": getattr(client, "cli_name", "filesystem"),
            "pre_tool_checked": pre_tool_callback is not None,
            "pre_tool_response": pre_tool_response,
        },
    )

    try:
        byte_count = await asyncio.to_thread(
            write_text_file, path, roots, content=params.get("content")
        )
    except ACPFileSystemError as exc:
        yield StreamEvent(
            event_type="tool_result",
            data={"call_id": call_id, "success": False, "result": None, "error": str(exc)},
        )
        await write_json_rpc_error(client, request.get("id"), code=exc.code, message=str(exc))
        return
    except OSError as exc:
        yield StreamEvent(
            event_type="tool_result",
            data={"call_id": call_id, "success": False, "result": None, "error": str(exc)},
        )
        await write_json_rpc_error(
            client,
            request.get("id"),
            code=-32000,
            message=f"failed to write file: {exc}",
        )
        return

    result = {"path": path, "bytes": byte_count}
    yield StreamEvent(
        event_type="tool_result",
        data={"call_id": call_id, "success": True, "result": result, "error": None},
    )
    await write_json_rpc_result(client, request.get("id"), None)


def _validate_session_id(client: Any, params: dict[str, Any]) -> str | None:
    session_id = params.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return "sessionId is required"
    current_session_id = getattr(client, "session_id", None)
    if (
        isinstance(current_session_id, str)
        and current_session_id
        and current_session_id != session_id
    ):
        return "sessionId does not match the active ACP session"
    return None


def is_pre_tool_decision_denied(response: Any) -> bool:
    decision = (
        response.get("decision")
        if isinstance(response, dict)
        else getattr(response, "decision", None)
    )
    return decision in _PRE_TOOL_DENY_DECISIONS


def pre_tool_denial_reason(response: Any) -> str:
    reason = (
        response.get("reason") if isinstance(response, dict) else getattr(response, "reason", None)
    )
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return "Blocked by Gobby before_tool policy"


async def _apply_pre_tool_decision(
    pre_tool_callback: PreToolCallback | None,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
) -> dict[str, Any] | None:
    if pre_tool_callback is None:
        return None
    try:
        return await pre_tool_callback({"tool_name": tool_name, "tool_input": tool_input})
    except Exception:
        logger.exception(
            "ACP pre-tool callback failed for %s with input keys %s",
            tool_name,
            sorted(str(key) for key in tool_input),
        )
        return {"decision": "deny", "reason": "Gobby pre-tool callback failed"}


def _permission_tool_details(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, dict):
        return "acp_tool", {}

    for key in ("name", "title", "kind"):
        value = tool_call.get(key)
        if isinstance(value, str) and value.strip():
            tool_name = value.strip()
            break
    else:
        tool_name = "acp_tool"

    raw_input = dict(tool_call)
    for input_key in ("rawInput", "input", "arguments"):
        if input_key in tool_call:
            raw_input = tool_call[input_key]
            break
    tool_input = raw_input if isinstance(raw_input, dict) else dict(tool_call)
    return tool_name, tool_input


# Permission-option kinds we treat as approval, in preference order. We prefer
# ``allow_once`` so Gobby is reconsulted on every tool call instead of the CLI
# permanently bypassing its own permission prompt for the rest of the session.
_PERMISSION_ALLOW_KINDS = ("allow_once", "allow_always")


def _select_permission_option(options: Any) -> tuple[str | None, str | None]:
    """Pick an auto-approval option from an ACP ``session/request_permission``.

    Managed web-chat ACP sessions approve tool calls through Gobby's own
    lifecycle/hook systems and the web UI, not the CLI's native interactive
    permission prompt. Auto-select an "allow" option so the agent executes the
    tool instead of treating the unanswered permission round-trip as a failure.

    Returns ``(option_id, kind)``; ``option_id`` is ``None`` when no allow-kind
    option is offered.
    """
    by_kind: dict[str, str] = {}
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            option_id = option.get("optionId")
            kind = option.get("kind")
            if isinstance(option_id, str) and isinstance(kind, str) and kind not in by_kind:
                by_kind[kind] = option_id
    for kind in _PERMISSION_ALLOW_KINDS:
        if kind in by_kind:
            return by_kind[kind], kind
    return None, None


async def _handle_request_permission_request(
    client: Any,
    request: dict[str, Any],
    *,
    pre_tool_callback: PreToolCallback | None = None,
) -> None:
    """Answer an ACP ``session/request_permission`` request.

    A spec-compliant ACP agent blocks the tool call on this request. If the client
    never answers — or answers with a JSON-RPC error — the Node CLI surfaces the
    rejected permission to the model as the literal string ``[object Object]`` and
    spirals into a runaway diagnostic loop (gobby #15705). Always return a well-formed
    outcome so the agent can proceed cleanly.
    """
    params = request.get("params")
    options = params.get("options") if isinstance(params, dict) else None
    option_id, kind = _select_permission_option(options)

    if option_id is None:
        # No allow option was offered: decline gracefully with a well-formed
        # ``cancelled`` outcome rather than erroring, so the agent records a
        # clean cancellation instead of an unrenderable error object.
        await write_json_rpc_result(
            client,
            request.get("id"),
            {"outcome": {"outcome": "cancelled"}},
        )
        return

    tool_name, tool_input = _permission_tool_details(params if isinstance(params, dict) else {})
    pre_tool_response = await _apply_pre_tool_decision(
        pre_tool_callback,
        tool_name=tool_name,
        tool_input=tool_input,
    )
    if is_pre_tool_decision_denied(pre_tool_response):
        reason = pre_tool_denial_reason(pre_tool_response)
        logger.debug(
            "%s ACP declined tool permission (%s): %s",
            getattr(client, "display_name", "ACP"),
            tool_name,
            reason,
        )
        await write_json_rpc_result(
            client,
            request.get("id"),
            {"outcome": {"outcome": "cancelled"}},
        )
        return

    tool_call = params.get("toolCall") if isinstance(params, dict) else None
    tool_title = tool_call.get("title") if isinstance(tool_call, dict) else None
    logger.debug(
        "%s ACP auto-approved tool permission (%s) via option %s",
        getattr(client, "display_name", "ACP"),
        tool_title or "tool",
        kind,
    )
    await write_json_rpc_result(
        client,
        request.get("id"),
        {"outcome": {"outcome": "selected", "optionId": option_id}},
    )


async def _handle_terminal_request(
    client: Any,
    request: dict[str, Any],
    *,
    pre_tool_callback: PreToolCallback | None = None,
) -> AsyncIterator[StreamEvent]:
    method = str(request.get("method") or "")
    params = request.get("params")
    if not isinstance(params, dict):
        await write_json_rpc_error(
            client,
            request.get("id"),
            code=-32602,
            message=f"{method} params must be an object",
        )
        return

    manager = getattr(client, "_terminal_manager", None)
    if manager is None:
        await write_json_rpc_error(
            client,
            request.get("id"),
            code=-32601,
            message="ACP terminal support is not available",
        )
        return

    if method == "terminal/create":
        async for event in _handle_terminal_create(
            client,
            request,
            params,
            pre_tool_callback=pre_tool_callback,
        ):
            yield event
        return

    terminal_id = str(params.get("terminalId") or "")
    if not terminal_id:
        await write_json_rpc_error(
            client,
            request.get("id"),
            code=-32602,
            message=f"{method} terminalId is required",
        )
        return

    try:
        if method == "terminal/output":
            result = await manager.output(terminal_id)
        elif method == "terminal/wait_for_exit":
            timeout = float(getattr(client, "_request_timeout", 30.0))
            try:
                result = await asyncio.wait_for(manager.wait_for_exit(terminal_id), timeout=timeout)
            except TimeoutError:
                await write_json_rpc_error(
                    client,
                    request.get("id"),
                    code=-32000,
                    message=f"{method} timed out after {timeout:.1f}s",
                )
                return
        elif method == "terminal/kill":
            result = await manager.kill(terminal_id)
        elif method == "terminal/release":
            result = await manager.release(terminal_id)
        else:
            raise TerminalNotFoundError(f"Unknown terminal method: {method}")
    except TerminalNotFoundError as exc:
        await write_json_rpc_error(client, request.get("id"), code=-32602, message=str(exc))
        return

    await write_json_rpc_result(client, request.get("id"), result)


async def _handle_terminal_create(
    client: Any,
    request: dict[str, Any],
    params: dict[str, Any],
    *,
    pre_tool_callback: PreToolCallback | None = None,
) -> AsyncIterator[StreamEvent]:
    manager = client._terminal_manager
    command = str(params.get("command") or "")
    if not command:
        await write_json_rpc_error(
            client,
            request.get("id"),
            code=-32602,
            message="terminal/create command is required",
        )
        return

    call_id = str(request.get("id") or f"terminal-{_make_id()}")
    tool_input = {
        "command": command,
        "args": params.get("args") or [],
        "cwd": params.get("cwd"),
        "env": params.get("env") or [],
        "outputByteLimit": params.get("outputByteLimit"),
    }
    pre_tool_response = await _apply_pre_tool_decision(
        pre_tool_callback,
        tool_name="run_terminal_command",
        tool_input=tool_input,
    )
    if is_pre_tool_decision_denied(pre_tool_response):
        await write_json_rpc_error(
            client,
            request.get("id"),
            code=-32000,
            message=pre_tool_denial_reason(pre_tool_response),
        )
        return

    try:
        result = await manager.create(params, default_cwd=getattr(client, "_cwd", None))
    except ValueError as exc:
        await write_json_rpc_error(client, request.get("id"), code=-32602, message=str(exc))
        return
    except OSError as exc:
        await write_json_rpc_error(client, request.get("id"), code=-32000, message=str(exc))
        return

    yield StreamEvent(
        event_type="tool_call",
        data={
            "call_id": call_id,
            "tool_name": "run_terminal_command",
            "tool_input": tool_input,
            "mcp_server": getattr(client, "cli_name", "terminal"),
            "pre_tool_checked": pre_tool_callback is not None,
        },
    )
    yield StreamEvent(
        event_type="tool_result",
        data={
            "call_id": call_id,
            "success": True,
            "result": result,
            "error": None,
        },
    )
    await write_json_rpc_result(client, request.get("id"), result)
