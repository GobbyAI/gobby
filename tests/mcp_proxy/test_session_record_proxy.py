"""An unmanaged MCP proxy speaks for its CLI's session through the record, never the machine key."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.utils.session_credential_record import (
    STORE_DIR,
    cli_pid_key,
    parent_is_codex_shared_host,
    record_path,
    session_ref_key,
)

pytestmark = pytest.mark.unit

CLI_PID = 4242
SESSION_ID = "11111111-2222-4333-8444-555555555555"
TOKEN = "session-token-for-the-bound-session"
MACHINE_KEY = {"Authorization": "Bearer machine-wide-key"}
_MANAGED_VARS = (
    "GOBBY_SESSION_ID",
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_AGENT_API_TOKEN",
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
)


def _response(status_code: int, payload: dict[str, object] | None = None) -> MagicMock:
    response = MagicMock(status_code=status_code, text="body")
    response.json.return_value = payload or {"success": True}
    return response


def _store_record(home: Path, key: str, payload: dict[str, str]) -> None:
    directory = home / STORE_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = record_path(home, key)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def unmanaged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """An unspawned CLI session whose CLI process is CLI_PID, with GOBBY_HOME isolated."""
    for name in _MANAGED_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    with patch(
        "gobby.utils.session_credential_record.stable_cli_pid",
        return_value=CLI_PID,
    ):
        yield tmp_path


def _client(*responses: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.request = AsyncMock(side_effect=list(responses))
    return client


@pytest.mark.asyncio
async def test_unmanaged_request_speaks_for_the_cli_session_without_the_machine_key(
    unmanaged: Path,
) -> None:
    _store_record(unmanaged, cli_pid_key(CLI_PID), {"token": TOKEN, "session_id": SESSION_ID})
    client = _client(_response(200, {"success": True}))

    with (
        patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers") as machine_key,
        patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient", return_value=client),
    ):
        proxy = DaemonProxy(60887)
        result = await proxy._request("POST", "/api/mcp/tools/call", json={})

    assert result == {"success": True}
    headers = client.request.await_args.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-Gobby-Session-Id"] == SESSION_ID
    assert "X-Gobby-Terminal-Context" not in headers
    machine_key.assert_not_called()


@pytest.mark.asyncio
async def test_unmanaged_request_without_a_record_refuses_before_any_request(
    unmanaged: Path,
) -> None:
    client = _client()

    with (
        patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers") as machine_key,
        patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient", return_value=client),
    ):
        proxy = DaemonProxy(60887)
        result = await proxy._request("POST", "/api/mcp/tools/call", json={})

    assert result["success"] is False
    assert result["error_code"] == "session_credential_unbound"
    client.request.assert_not_awaited()
    machine_key.assert_not_called()


def _parent_process(name: str, argv: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=lambda: name, cmdline=lambda: list(argv))


@pytest.mark.parametrize(
    ("name", "argv", "expected"),
    [
        ("codex", ["codex", "app-server", "--managed-daemon"], True),
        ("codex", ["codex", "resume", "--no-daemon", "thread"], False),
        ("zsh", ["zsh", "-c", "gmcp --managed-daemon"], False),
        ("bash", ["bash", "-lc", "codex app-server --managed-daemon"], False),
    ],
)
def test_only_a_proxy_the_shared_app_server_launched_counts_as_under_it(
    name: str, argv: list[str], expected: bool
) -> None:
    """A proxy a seat starts from its shell has a shell parent, so it is never the host's."""
    with (
        patch("gobby.utils.session_credential_record.os.getppid", return_value=CLI_PID),
        patch(
            "gobby.utils.session_credential_record.psutil.Process",
            return_value=_parent_process(name, argv),
        ),
    ):
        assert parent_is_codex_shared_host() is expected


@pytest.fixture
def under_shared_host() -> Iterator[None]:
    with patch(
        "gobby.utils.session_credential_record.parent_is_codex_shared_host", return_value=True
    ):
        yield


@pytest.mark.asyncio
async def test_a_proxy_outside_the_shared_host_never_selects_a_ref_record(
    unmanaged: Path,
) -> None:
    """A shell-started proxy naming any session ref presents no credential and sends nothing.

    Only the MCP client the shared app-server launched gets hook-gated calls, so only it may
    find a seat's record by the ref its call names.
    """
    _store_record(unmanaged, session_ref_key("#7"), {"token": TOKEN, "session_id": SESSION_ID})
    client = _client()

    with (
        patch(
            "gobby.utils.session_credential_record.parent_is_codex_shared_host",
            return_value=False,
        ),
        patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers") as machine_key,
        patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient", return_value=client),
    ):
        proxy = DaemonProxy(60887)
        result = await proxy._request("POST", "/api/mcp/tools/call", json={}, session_id="#7")

    assert result["error_code"] == "session_credential_unbound"
    client.request.assert_not_awaited()
    machine_key.assert_not_called()


@pytest.mark.asyncio
async def test_unmanaged_request_without_a_cli_record_speaks_for_its_own_session_ref(
    unmanaged: Path, under_shared_host: None
) -> None:
    """Under a shared host no record is kept per CLI; the call's own session ref finds it."""
    _store_record(unmanaged, session_ref_key("#7"), {"token": TOKEN, "session_id": SESSION_ID})
    client = _client(_response(200, {"success": True}))

    with (
        patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers") as machine_key,
        patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient", return_value=client),
    ):
        proxy = DaemonProxy(60887)
        result = await proxy._request("POST", "/api/mcp/tools/call", json={}, session_id="#7")

    assert result == {"success": True}
    headers = client.request.await_args.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-Gobby-Session-Id"] == SESSION_ID
    machine_key.assert_not_called()


@pytest.mark.asyncio
async def test_a_cli_record_wins_over_the_ref_a_call_names(unmanaged: Path) -> None:
    """A CLI-attested seat keeps its own credential whatever session ref a call carries."""
    _store_record(unmanaged, cli_pid_key(CLI_PID), {"token": TOKEN, "session_id": SESSION_ID})
    _store_record(
        unmanaged, session_ref_key("#8"), {"token": "other-token", "session_id": "other-session"}
    )
    client = _client(_response(200, {"success": True}))

    with (
        patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers"),
        patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient", return_value=client),
    ):
        proxy = DaemonProxy(60887)
        await proxy._request("POST", "/api/mcp/tools/call", json={}, session_id="#8")

    headers = client.request.await_args.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-Gobby-Session-Id"] == SESSION_ID


@pytest.mark.asyncio
async def test_unmanaged_request_naming_a_ref_with_no_record_is_refused(unmanaged: Path) -> None:
    client = _client()

    with (
        patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers") as machine_key,
        patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient", return_value=client),
    ):
        proxy = DaemonProxy(60887)
        result = await proxy._request("POST", "/api/mcp/tools/call", json={}, session_id="#9")

    assert result["error_code"] == "session_credential_unbound"
    client.request.assert_not_awaited()
    machine_key.assert_not_called()


def test_unmanaged_constructor_never_reads_the_machine_key(unmanaged: Path) -> None:
    with patch(
        "gobby.mcp_proxy.stdio_proxy.daemon_auth_headers",
        side_effect=AssertionError("the machine key must not be read"),
    ):
        proxy = DaemonProxy(60887)

    assert proxy._auth_headers == {}


@pytest.mark.asyncio
async def test_unmanaged_401_is_not_retried_with_the_machine_key(unmanaged: Path) -> None:
    _store_record(unmanaged, cli_pid_key(CLI_PID), {"token": TOKEN, "session_id": SESSION_ID})
    client = _client(_response(401))

    with (
        patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers") as machine_key,
        patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient", return_value=client),
    ):
        proxy = DaemonProxy(60887)
        result = await proxy._request("POST", "/api/mcp/tools/call", json={})

    assert result["success"] is False
    assert "401" in result["error"]
    assert client.request.await_count == 1
    machine_key.assert_not_called()


@pytest.mark.asyncio
async def test_managed_request_keeps_its_environment_session_and_agent_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in _MANAGED_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setenv("GOBBY_SESSION_ID", "spawn-session-uuid")
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-1")
    client = _client(_response(200, {"success": True}))

    with (
        patch(
            "gobby.mcp_proxy.stdio_proxy.daemon_auth_headers",
            return_value={"Authorization": "Bearer agent-token"},
        ) as agent_auth,
        patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient", return_value=client),
    ):
        proxy = DaemonProxy(60887)
        result = await proxy._request("POST", "/api/mcp/tools/call", json={})

    assert result == {"success": True}
    headers = client.request.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer agent-token"
    assert headers["X-Gobby-Session-Id"] == "spawn-session-uuid"
    agent_auth.assert_called_once_with()
