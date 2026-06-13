"""Regression tests for adapter subprocess stderr drains."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from gobby.adapters.acp_client import ACPClient
from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.adapters.codex_impl.types import CodexConnectionState

pytestmark = pytest.mark.unit

_STDERR_BYTES = 128 * 1024
_STDERR_TAIL = "stderr-tail-marker"


class _StubACPClient(ACPClient):
    cli_name = "stub-acp"
    display_name = "StubACP"
    prompt_timeout_env = "GOBBY_STUB_ACP_PROMPT_TIMEOUT_SECONDS"


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source)
    path.chmod(0o755)
    return path


def _stderr_burst_source(indent: str) -> str:
    lines = [
        'sys.stderr.write("stderr-head\\n")',
        "sys.stderr.flush()",
        f'sys.stderr.write("x" * {_STDERR_BYTES})',
        f'sys.stderr.write("\\n{_STDERR_TAIL}\\n")',
        "sys.stderr.flush()",
    ]
    return "\n".join(f"{indent}{line}" for line in lines)


def _fake_codex_turn_child_source() -> str:
    return f"""#!{sys.executable}
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        response = {{"jsonrpc": "2.0", "id": request_id, "result": {{"userAgent": "fake-codex/1.0"}}}}
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
    elif method == "turn/start":
{_stderr_burst_source("        ")}
        response = {{
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {{"turn": {{"id": "turn-1", "status": "inProgress", "items": []}}}},
        }}
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
"""


def _fake_acp_prompt_child_source() -> str:
    return f"""#!{sys.executable}
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        response = {{"jsonrpc": "2.0", "id": request_id, "result": {{"protocolVersion": 1}}}}
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
    elif method == "session/prompt":
{_stderr_burst_source("        ")}
        update = {{
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {{
                "sessionId": "session-1",
                "update": {{
                    "sessionUpdate": "agent_message_chunk",
                    "content": [{{"type": "text", "text": "ok"}}],
                }},
            }},
        }}
        final = {{"jsonrpc": "2.0", "id": request_id, "result": {{"stats": {{"tokens": 1}}}}}}
        sys.stdout.write(json.dumps(update) + "\\n")
        sys.stdout.write(json.dumps(final) + "\\n")
        sys.stdout.flush()
        break
"""


async def test_codex_app_server_drains_stderr_mid_turn_while_waiting_for_stdout(
    tmp_path: Path,
) -> None:
    """Codex turn/start finishes after the child writes more than a pipeful to stderr."""
    script = _write_executable(
        tmp_path / "fake-codex",
        _fake_codex_turn_child_source(),
    )
    client = CodexAppServerClient(codex_command=str(script))

    try:
        await asyncio.wait_for(client.start(), timeout=5.0)

        assert client.state == CodexConnectionState.CONNECTED
        turn = await asyncio.wait_for(client.start_turn("thread-1", "hello"), timeout=5.0)

        assert turn.id == "turn-1"
        assert await client._stderr_drain.wait_for_text(_STDERR_TAIL, timeout=1.0)
    finally:
        await client.stop()


async def test_acp_client_drains_stderr_mid_prompt_and_uses_ring_buffer_for_exit_diagnostics(
    tmp_path: Path,
) -> None:
    """ACP session/prompt finishes and exit diagnostics use the drained stderr tail."""
    script = _write_executable(
        tmp_path / "fake-acp",
        _fake_acp_prompt_child_source(),
    )
    client = _StubACPClient(cli_path=str(script), request_timeout=5.0)

    try:
        await asyncio.wait_for(client.start(auto_session=False), timeout=5.0)
        events = [event async for event in client.send("hello", session_id="session-1")]
        assert client._process is not None
        await asyncio.wait_for(client._process.wait(), timeout=5.0)

        stderr = await client._read_exit_stderr()

        assert [event.event_type for event in events] == ["content_delta", "result"]
        assert events[0].data["content"] == "ok"
        assert stderr is not None
        assert _STDERR_TAIL in stderr
    finally:
        await client.stop()
