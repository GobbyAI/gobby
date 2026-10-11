"""Live hook and wrapper traffic repairs a departed CLI's recorded identity."""

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch

import pytest

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
from tests.servers.routes.mcp_endpoints.test_execution_context import _make_request, _make_server

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("traffic", ["hook", "mcp", "none", "end", "unverified_mcp"])
async def test_expired_owner_recovers_only_from_live_session_traffic(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    traffic: str,
) -> None:
    machine_id = "21000000-0000-4000-8000-000000000009"
    with patch("gobby.utils.machine_id._cached_machine_id", machine_id):
        sessions = SessionManager(temp_db)
        owner = sessions.register(
            external_id="operator-pane",
            machine_id=machine_id,
            source="claude",
            project_id=sample_project["id"],
            terminal_context={"parent_pid": 2**30, "parent_create_time": 1.0},
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

        terminal_context = {"parent_pid": os.getpid()}
        if traffic == "unverified_mcp":
            terminal_context = {"parent_pid": 2**30}
        if traffic in {"mcp", "unverified_mcp"}:
            server = _make_server()
            server.session_manager = sessions
            request = _make_request(
                project_id=sample_project["id"],
                session_id=owner.id,
                wrapper=True,
                terminal_context=json.dumps(terminal_context),
            )
            with (
                patch("gobby.servers.routes.mcp.endpoints.request_context._bind_agent_run_context"),
                patch("gobby.servers.routes.mcp.endpoints.request_context._seed_request_principal"),
            ):
                tokens = await _set_context_for_request(cast(HTTPServer, server), {}, request)
                try:
                    assert tokens.resolved_session_id == owner.id
                finally:
                    reset_seeded_contexts(tokens)
        elif traffic in {"hook", "end"}:
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
                data={"terminal_context": terminal_context},
                machine_id=machine_id,
            )
            assert service.resolve(event) == owner.id
        else:
            sessions.revive_expired_terminal_session(owner.id)

        recovered = sessions.get(owner.id)
        assert recovered is not None
        live = traffic in {"hook", "mcp"}
        assert recovered.status == ("active" if live else "expired")
        if live:
            assert not recorded_process_exited(recovered)
            assert recovered.terminal_context is not None
            assert recovered.terminal_context["parent_pid"] == os.getpid()
        after = CoordinationWaitManager(temp_db).register(waiter.id, owner.id, reply=True)
        assert after["outcome"] == ("waiting" if live else "owner_ended")
