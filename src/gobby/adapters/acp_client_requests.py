"""Client-directed ACP JSON-RPC request handling."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from gobby.adapters.acp_client import StreamEvent, _make_id

DEFAULT_TERMINAL_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_TERMINAL_OUTPUT_BYTES = 200_000


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
    process.stdin.write((json.dumps(response) + "\n").encode())
    await process.stdin.drain()


async def write_json_rpc_error(client: Any, request_id: Any, *, code: int, message: str) -> None:
    process = getattr(client, "_process", None)
    if not process or not process.stdin:
        return
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    process.stdin.write((json.dumps(response) + "\n").encode())
    await process.stdin.drain()


async def handle_client_request(
    client: Any,
    request: dict[str, Any],
) -> AsyncIterator[StreamEvent]:
    method = request.get("method")
    if method == "terminal/create":
        async for event in _handle_terminal_create_request(client, request):
            yield event
        return

    await write_json_rpc_error(
        client,
        request.get("id"),
        code=-32601,
        message=f"Unknown client request method: {method}",
    )


async def _handle_terminal_create_request(
    client: Any,
    request: dict[str, Any],
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

    yield StreamEvent(
        event_type="tool_call",
        data={
            "call_id": call_id,
            "tool_name": "run_terminal_command",
            "tool_input": tool_input,
            "mcp_server": getattr(client, "cli_name", "terminal"),
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
    env = os.environ.copy()
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
