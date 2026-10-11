"""Live hook sender identity repairs a departed CLI's record before MCP lookup."""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from fastapi import Request

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.hooks.session_lookup import SessionLookupService
from gobby.hooks.session_types import HookSessionManager
from gobby.servers.http import HTTPServer
from gobby.servers.routes.mcp.endpoints.request_context import _set_context_for_request
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.terminal_ownership import recorded_process_exited
from gobby.utils.session_context import reset_seeded_contexts
from tests.mcp_proxy.test_stateless_stdio_proxy import _proxy_with_response, _success_response
from tests.servers.routes.mcp_endpoints.test_execution_context import _make_server

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "traffic",
    [
        "hook",
        "mcp_after_hook",
        "none",
        "end",
        "hook_without_context",
        "unverified_hook",
        pytest.param("native_hook", marks=pytest.mark.cli),
    ],
)
async def test_expired_owner_recovers_only_from_live_session_traffic(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    traffic: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_binary = os.environ.get("GOBBY_TEST_GHOOK")
    if traffic == "native_hook" and not native_binary:
        pytest.skip("Set GOBBY_TEST_GHOOK to the freshly built candidate ghook")
    machine_id = "21000000-0000-4000-8000-000000000009"
    with patch("gobby.utils.machine_id._cached_machine_id", machine_id):
        sessions = SessionManager(temp_db)
        owner = sessions.register(
            external_id="operator-pane",
            machine_id=machine_id,
            source="claude",
            project_id=sample_project["id"],
            terminal_context={
                "parent_pid": 2**30,
                "parent_create_time": 1.0,
                "gobby_terminal_id": "21000000-0000-4000-8000-000000000010",
            },
        )
        sessions.update_status(owner.id, "expired")
        expired = sessions.get(owner.id)
        assert expired is not None and recorded_process_exited(expired)
        waiter = sessions.register(
            external_id="coordination-waiter",
            machine_id=machine_id,
            source="claude",
            project_id=sample_project["id"],
        )
        before = CoordinationWaitManager(temp_db).register(waiter.id, owner.id, reply=True)
        assert before["outcome"] == "owner_ended"

        data: dict[str, Any] = {"terminal_context": {"parent_pid": os.getpid()}}
        if traffic == "native_hook":
            assert native_binary is not None
            (tmp_path / "machine_id").write_text(machine_id)
            native_input = {"session_id": owner.external_id, "cwd": str(tmp_path)}
            native_env = {
                **os.environ,
                "GOBBY_HOME": str(tmp_path),
                "GOBBY_PROJECT_ID": sample_project["id"],
            }
            native_env.pop("GOBBY_SESSION_ID", None)
            native_env.pop("GOBBY_AGENT_RUN_ID", None)
            process = await asyncio.create_subprocess_exec(
                native_binary,
                "--gobby-owned",
                "--cli",
                "claude",
                "--type",
                "pre-tool-use",
                "--enqueue-only",
                env=native_env,
                cwd=tmp_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(json.dumps(native_input).encode()),
                    timeout=10,
                )
            except (TimeoutError, asyncio.CancelledError):
                process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, (stdout, stderr)
            envelopes = list((tmp_path / "hooks" / "inbox").rglob("n-*.json"))
            assert len(envelopes) == 1
            data = json.loads(envelopes[0].read_text())["input_data"]
            assert data["machine_id"] == machine_id
            assert data["terminal_context"]["parent_pid"] == os.getpid()
        elif traffic == "hook_without_context":
            data = {}
        elif traffic == "unverified_hook":
            data = {"terminal_context": {"parent_pid": 2**30}}

        if traffic == "none":
            sessions.revive_expired_terminal_session(owner.id)
        else:
            hook_sessions = cast(HookSessionManager, sessions)
            service = SessionLookupService(
                session_manager=hook_sessions,
                session_coordinator=SessionCoordinator(session_storage=hook_sessions),
                session_task_manager=SessionTaskManager(temp_db),
                resolve_project_id=lambda *_: sample_project["id"],
                logger=logging.getLogger(__name__),
            )
            event = HookEvent(
                event_type=(
                    HookEventType.SESSION_END if traffic == "end" else HookEventType.BEFORE_TOOL
                ),
                session_id=owner.external_id,
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                data=data,
                machine_id=machine_id,
            )
            assert service.resolve(event) == owner.id

        recovered = sessions.get(owner.id)
        assert recovered is not None
        live = traffic in {"hook", "native_hook", "mcp_after_hook"}
        assert recovered.status == ("active" if live else "expired")
        if live:
            assert not recorded_process_exited(recovered)
            assert recovered.terminal_context is not None
            assert recovered.terminal_context["parent_pid"] == os.getpid()
        after = CoordinationWaitManager(temp_db).register(waiter.id, owner.id, reply=True)
        assert after["outcome"] == ("waiting" if live else "owner_ended")

        if traffic == "mcp_after_hook":
            monkeypatch.delenv("GOBBY_SESSION_ID", raising=False)
            with (
                patch(
                    "gobby.mcp_proxy.stdio_proxy.current_terminal_context",
                    return_value=recovered.terminal_context,
                ),
                patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers", return_value={}),
            ):
                proxy, outgoing = _proxy_with_response(_success_response())
            proxy._project_id = sample_project["id"]
            await proxy._request("GET", "/api/status")
            assert outgoing.await_args is not None
            headers = outgoing.await_args.kwargs["headers"]
            assert "X-Gobby-Session-Id" not in headers
            request = Request(
                {
                    "type": "http",
                    "headers": [
                        (name.lower().encode(), value.encode()) for name, value in headers.items()
                    ],
                }
            )
            server = _make_server()
            server.session_manager = sessions
            with (
                patch("gobby.servers.routes.mcp.endpoints.request_context._bind_agent_run_context"),
                patch("gobby.servers.routes.mcp.endpoints.request_context._seed_request_principal"),
            ):
                tokens = await _set_context_for_request(cast(HTTPServer, server), {}, request)
                try:
                    assert tokens.resolved_session_id == owner.id
                finally:
                    reset_seeded_contexts(tokens)
