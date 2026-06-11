"""Client-directed ACP JSON-RPC request handling."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from gobby.adapters.acp_client import StreamEvent, _make_id
from gobby.agents.constants import UV_CACHE_DIR

DEFAULT_TERMINAL_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_TERMINAL_OUTPUT_BYTES = 200_000
_TERMINAL_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", UV_CACHE_DIR)
logger = logging.getLogger(__name__)
PreToolCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
_PRE_TOOL_DENY_DECISIONS = {"deny", "block"}


def _coerce_terminal_timeout(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return DEFAULT_TERMINAL_REQUEST_TIMEOUT_SECONDS
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TERMINAL_REQUEST_TIMEOUT_SECONDS
    if parsed > 1_000:
        parsed = parsed / 1_000
    return min(max(parsed, 0.1), 300.0)


def _coerce_output_limit(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 20_000
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 20_000
    return min(max(parsed, 1_024), MAX_TERMINAL_OUTPUT_BYTES)


def _decode_limited(data: bytes, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(data)
    truncated = len(data) > limit
    clipped = data[:limit]
    return clipped.decode(errors="replace"), truncated


async def write_json_rpc_result(client: Any, request_id: Any, result: dict[str, Any]) -> None:
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
    if method == "terminal/create":
        async for event in _handle_terminal_create_request(
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
    return await pre_tool_callback({"tool_name": tool_name, "tool_input": tool_input})


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

    raw_input = tool_call.get("rawInput") or tool_call.get("input") or tool_call.get("arguments")
    tool_input = raw_input if isinstance(raw_input, dict) else dict(tool_call)
    return tool_name, tool_input


def _denied_terminal_result(reason: str) -> dict[str, Any]:
    return {
        "exitCode": 1,
        "stdout": "",
        "stderr": reason,
        "error": reason,
        "timedOut": False,
        "truncated": False,
        "cancelled": True,
    }


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

    A spec-compliant ACP agent (Gemini/Qwen) blocks the tool call on this
    request. If the client never answers — or answers with a JSON-RPC error —
    the Node CLI surfaces the rejected permission to the model as the literal
    string ``[object Object]`` and spirals into a runaway diagnostic loop
    (gobby #15705). Always return a well-formed outcome so the agent can
    proceed cleanly.
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


async def _handle_terminal_create_request(
    client: Any,
    request: dict[str, Any],
    *,
    pre_tool_callback: PreToolCallback | None = None,
) -> AsyncIterator[StreamEvent]:
    params = request.get("params")
    if not isinstance(params, dict):
        await write_json_rpc_error(
            client,
            request.get("id"),
            code=-32602,
            message="terminal/create params must be an object",
        )
        return

    command = str(params.get("command") or "").strip()
    if not command:
        await write_json_rpc_error(
            client,
            request.get("id"),
            code=-32602,
            message="terminal/create command is required",
        )
        return

    cwd = str(params.get("cwd") or getattr(client, "_cwd", None) or ".")
    timeout_seconds = _coerce_terminal_timeout(params.get("timeout"))
    output_limit = _coerce_output_limit(params.get("outputByteLimit"))
    call_id = str(request.get("id") or f"terminal-{_make_id()}")
    tool_input = {
        "command": command,
        "cwd": cwd,
        "timeout": timeout_seconds,
        "outputByteLimit": output_limit,
    }
    pre_tool_response = await _apply_pre_tool_decision(
        pre_tool_callback,
        tool_name="run_terminal_command",
        tool_input=tool_input,
    )
    if is_pre_tool_decision_denied(pre_tool_response):
        result = _denied_terminal_result(pre_tool_denial_reason(pre_tool_response))
        await write_json_rpc_result(client, request.get("id"), result)
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

    result = await _run_terminal_create(
        command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        output_limit=output_limit,
    )
    success = result.get("exitCode") == 0 and not result.get("timedOut")
    yield StreamEvent(
        event_type="tool_result",
        data={
            "call_id": call_id,
            "success": success,
            "result": result,
            "error": None if success else result.get("stderr") or result.get("error"),
        },
    )
    await write_json_rpc_result(client, request.get("id"), result)


async def _run_terminal_create(
    command: str,
    *,
    cwd: str,
    timeout_seconds: float,
    output_limit: int,
) -> dict[str, Any]:
    env = {key: os.environ[key] for key in _TERMINAL_ENV_ALLOWLIST if key in os.environ}
    env["GOBBY_HOOKS_DISABLED"] = "1"
    env["GOBBY_ACP_CHILD_TOOL"] = "1"

    try:
        resolved_cwd = str(Path(cwd).expanduser().resolve())
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=resolved_cwd,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
            timed_out = False
        except TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            timed_out = True
    except Exception as exc:
        return {
            "exitCode": 1,
            "stdout": "",
            "stderr": "",
            "error": str(exc),
            "timedOut": False,
            "truncated": False,
        }

    stdout_text, stdout_truncated = _decode_limited(stdout, output_limit)
    stderr_limit = max(0, output_limit - len(stdout_text.encode("utf-8", errors="replace")))
    stderr_text, stderr_truncated = _decode_limited(stderr, stderr_limit)
    return {
        "exitCode": proc.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "timedOut": timed_out,
        "truncated": stdout_truncated or stderr_truncated,
    }
