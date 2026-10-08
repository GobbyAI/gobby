"""A spawned pane's frozen run identity follows its run to the /clear successor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from gobby.config.app import DaemonConfig
from gobby.hooks.runtime_compat import SUPPORTED_HOOK_RESPONSE_CAPABILITY
from gobby.mcp_proxy.wait_tools import MCP_WRAPPER_PROTOCOL_VERSION_HEADER
from gobby.runtime_grants import encode_grant_header
from gobby.servers.auth_service import AuthService
from gobby.servers.lease_fence import EffectFence
from gobby.servers.middleware.auth import AuthMiddleware
from gobby.servers.routes.llm import create_llm_router
from gobby.servers.routes.mcp.endpoints.request_context import _set_context_for_request
from gobby.sessions.clear_continuation import take_clear_handoff_marker
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token
from gobby.utils.session_context import get_session_context, reset_seeded_contexts
from gobby.workflows.state_manager import SessionVariableManager
from tests.hooks.test_clear_successor_seat import _pane, _spawned_seat, _stage
from tests.hooks.test_session_materialize import _ATTEMPT_ID, _StagedClear
from tests.servers.conftest import create_http_server
from tests.servers.routes.test_llm_routes import (
    _chat_result,
    _FakeToolChatService,
    _valid_tool_chat_payload,
)
from tests.servers.test_auth_service import (
    _GRANT_HEADER,
    _GRANT_NOW,
    _MACHINE_HEADER,
    _grant_service,
    _request,
    _signed_presentation_grant,
    _write_bootstrap,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

pytestmark = pytest.mark.unit

_API_KEY = "managed-clear-identity"
_PROBE = "seat_probe"
_OTHER_PROJECT = "31000000-0000-4000-8000-000000000001"


async def _run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


def _token(pane: _StagedClear, run_id: str, session_id: str) -> str:
    return issue_agent_api_token(
        derive_managed_signing_key(_API_KEY),
        agent_run_id=run_id,
        session_id=session_id,
        project_id=pane.project_id,
        machine_id=pane.machine_id,
    )


def _frozen_headers(token: str, run_id: str, session_id: str, project_id: str) -> dict[str, str]:
    """The run identity a spawned pane sends from its launch environment."""
    return {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Agent-Run-Id": run_id,
        "X-Gobby-Session-Id": session_id,
        "X-Gobby-Caller-Project-Id": project_id,
        "X-Gobby-Project-Id": project_id,
    }


def _tool_call(headers: dict[str, str]) -> Request:
    return _request(
        {**headers, MCP_WRAPPER_PROTOCOL_VERSION_HEADER: "1"},
        method="POST",
        path="/api/mcp/tools/call",
    )


@dataclass
class _Seat:
    """A spawned pane whose run and launch identity cross one staged /clear."""

    pane: _StagedClear
    run: AgentRun
    service: AuthService
    token: str
    successor_id: str = ""

    @property
    def db(self) -> HubDatabase:
        return self.pane.sessions.db

    def headers(self) -> dict[str, str]:
        """The predecessor's identity, which /clear leaves frozen in the pane."""
        return _frozen_headers(
            self.token, self.run.id, self.pane.predecessor_id, self.pane.project_id
        )

    def take(self) -> None:
        """The successor's SessionStart takes the clear marker and, with it, the run."""
        self.successor_id = self.pane.register("successor-ext")
        assert take_clear_handoff_marker(
            self.db,
            self.pane.predecessor_id,
            attempt_id=_ATTEMPT_ID,
            successor_id=self.successor_id,
        )

    def end_run(self) -> None:
        assert LocalAgentRunManager(self.db).complete(self.run.id) is not None

    def server(self, **attrs: Any) -> HTTPServer:
        return cast(
            "HTTPServer",
            SimpleNamespace(
                auth_service=self.service,
                session_manager=self.pane.sessions,
                run_db=_run_db,
                **attrs,
            ),
        )

    async def caller(self, headers: dict[str, str]) -> str | None:
        """The session a wrapper tool call carrying ``headers`` is attributed to."""
        tokens = await _set_context_for_request(self.server(), {}, _tool_call(headers))
        reset_seeded_contexts(tokens)
        return tokens.resolved_session_id


@pytest.fixture
def seat(temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Seat:
    pane = _pane(temp_db, tmp_path, monkeypatch, "managed")
    run, _terminal = _spawned_seat(temp_db, pane)
    _stage(temp_db, pane)
    service = AuthService(
        lambda: temp_db,
        bootstrap_file=_write_bootstrap(tmp_path / "bootstrap.yaml", _API_KEY),
        break_glass_file=tmp_path / "absent-break-glass",
    )
    service.bind_runtime(
        grant_service=_grant_service(),
        lease_live=lambda: True,
        local_machine_id=pane.machine_id,
        effect_fence=EffectFence(),
        clock=lambda: _GRANT_NOW + 10,
    )
    return _Seat(
        pane=pane, run=run, service=service, token=_token(pane, run.id, pane.predecessor_id)
    )


async def test_successor_tool_call_is_attributed_to_successor(seat: _Seat) -> None:
    seat.take()
    request = _tool_call(seat.headers())
    assert seat.service.authenticate(request).allowed

    tokens = await _set_context_for_request(seat.server(), {}, request)
    try:
        caller = get_session_context()
        assert caller is not None
        SessionVariableManager(seat.db).set_variable(caller.session_id, _PROBE, "written")
    finally:
        reset_seeded_contexts(tokens)

    variables = SessionVariableManager(seat.db)
    assert tokens.resolved_session_id == seat.successor_id
    assert variables.get_variables(seat.successor_id).get(_PROBE) == "written"
    assert _PROBE not in variables.get_variables(seat.pane.predecessor_id)


def _hook_server(seat: _Seat) -> HTTPServer:
    """The daemon's routes behind real auth, with the provider adapter left to the test."""
    server = create_http_server(session_manager=seat.pane.sessions, authenticated_requests=False)
    server.auth_service = seat.service
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()
    return server


def _hook(hook_type: str, conversation_id: str, **input_data: str) -> dict[str, Any]:
    """A Claude hook envelope from the CLI conversation ``conversation_id``."""
    return {
        "schema_version": 1,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "enqueued_at": "2026-10-08T12:00:00Z",
        "critical": False,
        "hook_type": hook_type,
        "source": "claude",
        "input_data": {"session_id": conversation_id, **input_data},
    }


def test_successor_hook_and_variables_follow_run(seat: _Seat) -> None:
    seat.take()
    variables = SessionVariableManager(seat.db)
    variables.set_variable(seat.pane.predecessor_id, _PROBE, "predecessor")
    variables.set_variable(seat.successor_id, _PROBE, "successor")

    with (
        TestClient(_hook_server(seat).app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        adapter_cls.return_value.handle_native.return_value = {"continue": True}
        hook = client.post(
            "/api/hooks/execute",
            json=_hook("session-start", "successor-ext"),
            headers=seat.headers(),
        )
        read = client.post(
            f"/api/sessions/{seat.pane.predecessor_id}/variables/get",
            json={"name": _PROBE},
            headers=seat.headers(),
        )

    assert hook.status_code == 200, hook.text
    adapter_payload = adapter_cls.return_value.handle_native.call_args.args[0]
    assert adapter_payload["_platform_session_id"] == seat.successor_id
    assert read.status_code == 200, read.text
    assert read.json()["session_id"] == seat.successor_id
    assert read.json()["value"] == "successor"


@pytest.mark.parametrize(
    ("conversation_id", "owner"),
    [("pred-ext", "predecessor"), ("successor-ext", "successor")],
)
def test_late_predecessor_hook_stays_on_predecessor(
    seat: _Seat, conversation_id: str, owner: str
) -> None:
    """A clear end the predecessor's conversation sends after the take cannot end the seat."""
    seat.take()

    with (
        TestClient(_hook_server(seat).app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        adapter_cls.return_value.handle_native.return_value = {"continue": True}
        response = client.post(
            "/api/hooks/execute",
            json=_hook("session-end", conversation_id, reason="clear"),
            headers=seat.headers(),
        )

    assert response.status_code == 200, response.text
    adapter_payload = adapter_cls.return_value.handle_native.call_args.args[0]
    owners = {"predecessor": seat.pane.predecessor_id, "successor": seat.successor_id}
    assert adapter_payload["_platform_session_id"] == owners[owner]


async def test_no_forwarding_without_live_run_binding(seat: _Seat) -> None:
    from gobby.sessions.clear_run_lineage import current_run_session_id

    pane = seat.pane
    assert await seat.caller(seat.headers()) == pane.predecessor_id

    seat.take()
    runs = LocalAgentRunManager(seat.db)
    other_id = pane.register("other-ext")
    created = runs.create(
        parent_session_id=seat.run.parent_session_id,
        provider="claude",
        prompt="Other.",
        child_session_id=other_id,
    )
    other = runs.start(created.id)
    assert other is not None
    seat.db.execute("UPDATE sessions SET agent_run_id = %s WHERE id = %s", (other.id, other_id))
    other_headers = _frozen_headers(
        _token(pane, other.id, other_id), other.id, other_id, pane.project_id
    )
    assert await seat.caller(other_headers) == other_id
    assert (
        current_run_session_id(
            seat.db, agent_run_id=None, session_id=pane.predecessor_id, project_id=pane.project_id
        )
        == pane.predecessor_id
    )
    assert (
        current_run_session_id(
            seat.db,
            agent_run_id=seat.run.id,
            session_id=pane.predecessor_id,
            project_id=_OTHER_PROJECT,
        )
        == pane.predecessor_id
    )

    seat.end_run()
    assert seat.service.authenticate(_tool_call(seat.headers())).code == "run_inactive"
    with pytest.raises(HTTPException) as refused:
        await seat.caller(seat.headers())
    assert refused.value.status_code == 403
    assert (
        current_run_session_id(
            seat.db,
            agent_run_id=seat.run.id,
            session_id=pane.predecessor_id,
            project_id=pane.project_id,
        )
        == pane.predecessor_id
    )


def test_successor_chat_completion_keeps_issued_grant(seat: _Seat) -> None:
    seat.take()
    pane = seat.pane
    grant = _signed_presentation_grant(
        kind="agent_run",
        machine_id=pane.machine_id,
        project_id=pane.project_id,
        session_id=pane.predecessor_id,
        execution_id=seat.run.id,
    )
    headers = {
        **seat.headers(),
        _GRANT_HEADER: encode_grant_header(grant),
        _MACHINE_HEADER: pane.machine_id,
    }
    tool_chat = _FakeToolChatService(_chat_result())
    server = seat.server(
        config=DaemonConfig(), services=SimpleNamespace(tool_chat_service=tool_chat)
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)
    app.include_router(create_llm_router(server))
    client = TestClient(app)

    served = client.post(
        "/api/llm/chat/completions", json=_valid_tool_chat_payload(), headers=headers
    )

    assert served.status_code == 200, served.text
    assert [request.session_id for request in tool_chat.requests] == [UUID(seat.successor_id)]

    seat.end_run()
    refused = client.post(
        "/api/llm/chat/completions", json=_valid_tool_chat_payload(), headers=headers
    )

    assert refused.status_code == 401
    assert len(tool_chat.requests) == 1
    assert seat.service.authenticate(_tool_call(seat.headers())).code == "run_inactive"
