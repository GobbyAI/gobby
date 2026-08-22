from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.stdio import DaemonProxy

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_managed_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests hermetic when the suite itself runs inside a managed agent."""
    monkeypatch.delenv("GOBBY_AGENT_RUN_ID", raising=False)
    monkeypatch.delenv("GOBBY_SESSION_ID", raising=False)
    monkeypatch.delenv("GOBBY_AGENT_API_TOKEN", raising=False)


def test_project_id_reads_nearest_parent_project_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    nested = project_root / "src" / "pkg"
    nested.mkdir(parents=True)
    (project_root / ".gobby").mkdir()
    (project_root / ".gobby" / "project.json").write_text(json.dumps({"id": "project-from-parent"}))
    monkeypatch.chdir(nested)
    monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)

    proxy = DaemonProxy(60887)

    assert proxy._project_id == "project-from-parent"


def test_project_id_env_overrides_project_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".gobby").mkdir()
    (tmp_path / ".gobby" / "project.json").write_text(json.dumps({"id": "project-from-file"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOBBY_PROJECT_ID", "project-from-env")

    proxy = DaemonProxy(60887)

    assert proxy._project_id == "project-from-env"


def _mock_response(payload: dict[str, Any] | None = None) -> MagicMock:
    response = MagicMock(status_code=200)
    response.json.return_value = payload or {"success": True}
    return response


def _runtime_with_timeout_config() -> MagicMock:
    runtime = MagicMock()
    runtime.require_config.return_value = MagicMock(mcp_client_proxy=MagicMock(tool_timeouts={}))
    return runtime


def _mock_http_client(
    mock_client_cls: MagicMock, payload: dict[str, Any] | None = None
) -> AsyncMock:
    client = AsyncMock()
    client.request = AsyncMock(return_value=_mock_response(payload))
    mock_client_cls.return_value = client
    return client


@pytest.mark.asyncio
async def test_request_session_id_override_does_not_mutate_proxy_state() -> None:
    proxy = DaemonProxy(60887)

    with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
        client = _mock_http_client(mock_client_cls)

        await proxy._request(
            "POST",
            "/some/path",
            json={"session_id": "target-session"},
            session_id="new-session",
        )

    _, kwargs = client.request.call_args
    assert kwargs["headers"]["X-Gobby-Session-Id"] == "new-session"
    assert not hasattr(proxy, "_session_id")


@pytest.mark.asyncio
async def test_request_body_session_id_does_not_update_context_header() -> None:
    proxy = DaemonProxy(60887)

    with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
        client = _mock_http_client(mock_client_cls)

        await proxy._request("POST", "/some/path", json={"session_id": "target-session"})

    _, kwargs = client.request.call_args
    assert "X-Gobby-Session-Id" not in kwargs["headers"]
    assert not hasattr(proxy, "_session_id")


@pytest.mark.asyncio
async def test_call_tool_session_id_override_does_not_mutate_proxy_state() -> None:
    proxy = DaemonProxy(60887)

    with patch(
        "gobby.mcp_proxy.stdio.CliRuntime",
        return_value=_runtime_with_timeout_config(),
    ):
        with patch(
            "gobby.mcp_proxy.stdio.check_daemon_http_health",
            new_callable=AsyncMock,
        ) as mock_health:
            mock_health.return_value = True
            with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
                client = _mock_http_client(mock_client_cls)

                await proxy.call_tool(
                    "gobby-tasks",
                    "list_tasks",
                    {"status": "open"},
                    session_id="new-session",
                )

    _, kwargs = client.request.call_args
    assert kwargs["headers"]["X-Gobby-Session-Id"] == "new-session"
    assert kwargs["json"] == {"status": "open"}
    assert not hasattr(proxy, "_session_id")


@pytest.mark.asyncio
async def test_call_tool_sends_caller_project_header_with_target_project_override() -> None:
    proxy = DaemonProxy(60887)
    proxy._project_id = "caller-project"

    with patch(
        "gobby.mcp_proxy.stdio.CliRuntime",
        return_value=_runtime_with_timeout_config(),
    ):
        with patch(
            "gobby.mcp_proxy.stdio.check_daemon_http_health",
            new_callable=AsyncMock,
        ) as mock_health:
            mock_health.return_value = True
            with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
                client = _mock_http_client(mock_client_cls)

                await proxy.call_tool(
                    "gobby-tasks",
                    "list_tasks",
                    {},
                    project_id="target-project",
                    session_id="#7",
                )

    _, kwargs = client.request.call_args
    assert kwargs["headers"]["X-Gobby-Project-Id"] == "target-project"
    assert kwargs["headers"]["X-Gobby-Caller-Project-Id"] == "caller-project"
    assert kwargs["headers"]["X-Gobby-Session-Id"] == "#7"


@pytest.mark.asyncio
async def test_managed_agent_pins_session_header_to_spawn_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_SESSION_ID", "spawn-session-uuid")
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-123")
    proxy = DaemonProxy(60887)

    with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
        client = _mock_http_client(mock_client_cls)

        await proxy._request(
            "POST",
            "/some/path",
            json={"session_id": "#42"},
            session_id="#42",
        )

    _, kwargs = client.request.call_args
    assert kwargs["headers"]["X-Gobby-Session-Id"] == "spawn-session-uuid"
    assert kwargs["headers"]["X-Gobby-Agent-Run-Id"] == "run-123"
    # The body session_id stays a target-tool argument, untouched by pinning.
    assert kwargs["json"] == {"session_id": "#42"}


@pytest.mark.asyncio
async def test_variable_tools_send_requested_session_as_target_and_header() -> None:
    proxy = DaemonProxy(60887)

    with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
        client = _mock_http_client(mock_client_cls)

        await proxy.set_variable("flag", True, session_id="new-session")
        await proxy.get_variable("flag", session_id="new-session")

    set_call, get_call = client.request.call_args_list
    assert set_call.kwargs["headers"]["X-Gobby-Session-Id"] == "new-session"
    assert set_call.args[1].endswith("/api/sessions/new-session/variables/set")
    assert set_call.kwargs["json"] == {"name": "flag", "value": True, "scope": "session"}
    assert get_call.kwargs["headers"]["X-Gobby-Session-Id"] == "new-session"
    assert get_call.args[1].endswith("/api/sessions/new-session/variables/get")
    assert get_call.kwargs["json"] == {"name": "flag", "scope": "session"}
    assert not hasattr(proxy, "_session_id")
