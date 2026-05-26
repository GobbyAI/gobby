"""Tests for client-directed ACP JSON-RPC request handling."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.adapters import acp_client_requests
from gobby.agents.constants import UV_CACHE_DIR

pytestmark = pytest.mark.unit


class _ClosedStdin:
    def write(self, _data: bytes) -> None:
        raise BrokenPipeError


@pytest.mark.asyncio
async def test_write_json_rpc_result_ignores_closed_client_pipe() -> None:
    """A closed ACP client pipe is ignored when sending a JSON-RPC result."""
    client = SimpleNamespace(_process=SimpleNamespace(stdin=_ClosedStdin()))

    await acp_client_requests.write_json_rpc_result(client, "request-1", {"ok": True})


@pytest.mark.asyncio
async def test_terminal_create_env_uses_minimal_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Terminal requests preserve only the minimal env allowlist plus ACP guard vars."""
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"ok", b""

    async def fake_create_subprocess_shell(*_args: Any, **kwargs: Any) -> FakeProcess:
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv(UV_CACHE_DIR, "/tmp/gobby-uv-cache")
    monkeypatch.setenv("SECRET_TOKEN", "should-not-leak")
    monkeypatch.setattr(
        acp_client_requests.asyncio,
        "create_subprocess_shell",
        fake_create_subprocess_shell,
    )

    result = await acp_client_requests._run_terminal_create(
        "printf ok",
        cwd=str(tmp_path),
        timeout_seconds=1.0,
        output_limit=1024,
    )

    assert result["exitCode"] == 0
    env = captured["env"]
    assert env["PATH"] == "/usr/bin"
    assert env[UV_CACHE_DIR] == "/tmp/gobby-uv-cache"
    assert env["GOBBY_HOOKS_DISABLED"] == "1"
    assert env["GOBBY_ACP_CHILD_TOOL"] == "1"
    assert "SECRET_TOKEN" not in env
