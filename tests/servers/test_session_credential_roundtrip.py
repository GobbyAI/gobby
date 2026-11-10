"""SessionStart credential transport through real authenticated MCP and hook routes."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    SUPPORTED_HOOK_RESPONSE_CAPABILITY,
)
from gobby.hooks.session_claimant import Claimant
from gobby.hooks.session_credential import SESSION_CREDENTIAL_FIELD
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.servers.auth_service import AuthService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.local_token import derive_managed_signing_key, issue_session_api_token
from gobby.utils.session_context import get_session_context
from gobby.utils.session_credential_record import cli_pid_key, record_path
from tests.fixtures.isolated_checkout import install_isolated_checkout_project
from tests.servers.conftest import create_http_server

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("in_tmux", [False, True])
async def test_minted_token_completes_mcp_and_hook_without_machine_credentials(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    in_tmux: bool,
) -> None:
    daemon_home = tmp_path / "daemon"
    client_home = tmp_path / "client"
    daemon_home.mkdir()
    client_home.mkdir()
    bootstrap = daemon_home / "bootstrap.yaml"
    bootstrap.write_text(json.dumps({"api_key": "isolated-roundtrip-machine-key"}))
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    for name in (
        "GOBBY_AGENT_API_TOKEN",
        "GOBBY_AGENT_RUN_ID",
        "GOBBY_SESSION_ID",
        "GOBBY_MANAGED_EXECUTION_ID",
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOBBY_HOME", str(client_home))
    if in_tmux:
        monkeypatch.setenv("TMUX", "/tmp/isolated-tmux,123,0")
        monkeypatch.setenv("TMUX_PANE", "%77")
    else:
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr("gobby.utils.session_credential_record.stable_cli_pid", os.getpid)
    sessions = SessionManager(temp_db)
    checkout = install_isolated_checkout_project(
        temp_db, tmp_path / "checkout", machine_id=MACHINE_ID
    )
    project_id = checkout.project.id
    session = sessions.register(
        external_id="roundtrip-provider",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=project_id,
    )
    temp_db.execute(
        "UPDATE sessions SET terminal_context = %s::jsonb WHERE id = %s",
        (json.dumps({"parent_pid": os.getpid()}), session.id),
    )
    server = create_http_server(
        database=temp_db,
        session_manager=sessions,
        project_id=project_id,
        authenticated_requests=False,
    )
    server.app.state.server = server
    server.auth_service = AuthService(
        lambda: temp_db,
        bootstrap_file=bootstrap,
        break_glass_file=daemon_home / "absent",
    )
    server.auth_service.bind_runtime(
        grant_service=None,
        local_machine_id=MACHINE_ID,
        clock=None,
        front_door_secret="isolated-front-door",
    )
    manager = HookManager(database=temp_db, session_manager=sessions, config=DaemonConfig())
    server.app.state.hook_manager = manager
    calls: list[str | None] = []

    def echo() -> dict[str, Any]:
        context = get_session_context()
        caller = context.session_id if context is not None else None
        calls.append(caller)
        return {"success": True, "caller": caller}

    registry = InternalToolRegistry("synthetic-roundtrip")
    registry.register("echo", "Return the authenticated caller", {"type": "object"}, echo)
    internal = InternalRegistryManager()
    internal.add_registry(registry)
    server._internal_manager = internal
    mcp = MagicMock(spec=MCPClientManager)
    mcp.session_manager = sessions
    mcp.project_id = project_id
    proxy_service = ToolProxyService(
        mcp, internal_manager=internal, hook_manager_resolver=lambda: manager
    )
    server._tools_handler = cast(GobbyDaemonTools, SimpleNamespace(tool_proxy=proxy_service))
    envelope = {
        "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "critical": False,
        "source": "claude",
        "hook_type": "session-start",
        "input_data": {
            "session_id": session.external_id,
            "machine_id": MACHINE_ID,
            "source": "startup",
            "cwd": checkout.root_path,
        },
    }
    stack = ExitStack()
    try:
        start_client = stack.enter_context(TestClient(server.app))
        with patch(
            "gobby.hooks.session_claimant.front_door_claimant",
            return_value=Claimant(pid=os.getpid() + 1, parent_pid=os.getpid(), cli_pid=os.getpid()),
        ):
            start = start_client.post(
                "/api/hooks/execute",
                json=envelope,
                headers={
                    "X-Gobby-Front-Door": "isolated-front-door",
                    "X-Gobby-User-Id": "isolated-operator",
                    "X-Gobby-Machine-Id": MACHINE_ID,
                    "X-Gobby-Key-Id": "isolated-key",
                    "X-Gobby-Peer-Port": "12345",
                },
            )
        assert start.status_code == 200, start.text
        credential = start.json()[SESSION_CREDENTIAL_FIELD]
        path = record_path(client_home, cli_pid_key(os.getpid()))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(credential))
        path.chmod(0o600)
        # The client has only the delivered session record: no bootstrap or machine bearer.
        assert not (client_home / "bootstrap.yaml").exists()
        assert not (client_home / "local_cli_token").exists()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://localhost"
        ) as client:
            proxy = DaemonProxy(60887)
            proxy._project_id = project_id
            proxy._client = client
            with patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers") as machine_key:
                result = await proxy._request(
                    "POST",
                    "/api/mcp/tools/call",
                    json={
                        "server_name": "synthetic-roundtrip",
                        "tool_name": "echo",
                        "arguments": {},
                    },
                )
            assert result["success"], result
            assert calls == [session.id]
            machine_key.assert_not_called()
            hook = await client.post(
                "/api/hooks/execute",
                json={**envelope, "hook_type": "pre-tool-use"},
                headers={
                    "Authorization": f"Bearer {credential['token']}",
                    "X-Gobby-Session-Id": session.id,
                    "X-Gobby-Caller-Project-Id": project_id,
                    "X-Gobby-Project-Id": project_id,
                },
            )
            assert hook.status_code == 200, hook.text
            assert hook.json() == {"continue": True}, hook.text
            headers = {
                "Authorization": f"Bearer {credential['token']}",
                "X-Gobby-Session-Id": session.id,
                "X-Gobby-Project-Id": project_id,
            }
            accepted = await client.get("/api/mcp/status", headers=headers)
            assert accepted.status_code == 200, accepted.text
            old_token = issue_session_api_token(
                derive_managed_signing_key("isolated-roundtrip-machine-key"),
                session_id=session.id,
                project_id=project_id,
                machine_id=MACHINE_ID,
            )
            refused = await client.get(
                "/api/mcp/status",
                headers=headers | {"Authorization": f"Bearer {old_token}"},
            )
            assert refused.status_code == 401, refused.text
            assert refused.json()["code"] == "capability_invalid"
            assert old_token not in refused.text
    finally:
        stack.close()
        manager.shutdown()
