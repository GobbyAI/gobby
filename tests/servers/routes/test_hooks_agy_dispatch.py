"""Focused tests for AGY hook dispatch through the unified hooks route."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import StartupContextClaim
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def _agy_pre_invocation_envelope(*, conversation_id: str = "agy-conv-1") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enqueued_at": "2026-06-24T12:00:00Z",
        "critical": False,
        "hook_type": "PreInvocation",
        "source": "agy",
        "input_data": {
            "hookEventName": "PreInvocation",
            "conversationId": conversation_id,
            "workspacePaths": ["/tmp/agy-ws"],
            "cwd": "/tmp/agy-ws",
        },
    }


@pytest.fixture
def session_storage(temp_db: HubDatabase) -> SessionManager:
    return SessionManager(temp_db)


def test_execute_hook_dispatches_agy_adapter(session_storage: SessionManager) -> None:
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_storage,
    )
    mock_hook_manager = MagicMock()
    mock_hook_manager.shutdown_async = AsyncMock()
    server.app.state.hook_manager = mock_hook_manager

    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.agy.AgyAdapter") as MockAdapter,
    ):
        mock_adapter = MagicMock()
        mock_adapter.handle_native.return_value = {"decision": "allow"}
        MockAdapter.return_value = mock_adapter

        response = client.post(
            "/api/hooks/execute",
            json={
                "schema_version": 1,
                "enqueued_at": "2026-06-24T12:00:00Z",
                "critical": False,
                "hook_type": "PreToolUse",
                "source": "agy",
                "input_data": {
                    "hook_event_name": "PreToolUse",
                    "session_id": "agy-123",
                    "cwd": "/tmp",
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == {"decision": "allow"}
    MockAdapter.assert_called_once()
    adapter_hook_manager = MockAdapter.call_args.kwargs["hook_manager"]
    assert adapter_hook_manager is server.app.state.hook_manager
    assert mock_adapter.handle_native.call_args.args[0] == {
        "hook_type": "PreToolUse",
        "source": "agy",
        "input_data": {
            "hook_event_name": "PreToolUse",
            "session_id": "agy-123",
            "cwd": "/tmp",
        },
    }
    assert mock_adapter.handle_native.call_args.args[1] is adapter_hook_manager


def test_execute_hook_unsupported_source_lists_agy(
    session_storage: SessionManager,
) -> None:
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_storage,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    with TestClient(server.app) as client:
        response = client.post(
            "/api/hooks/execute",
            json={
                "schema_version": 1,
                "enqueued_at": "2026-06-24T12:00:00Z",
                "critical": False,
                "hook_type": "PreToolUse",
                "source": "unsupported",
                "input_data": {},
            },
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "Unsupported source" in detail
    assert "agy" in detail


class TestAgyStartupClaimPreflight:
    def test_preinvocation_claims_startup_context_before_adapter_runs(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        matching = SimpleNamespace(
            id="sess-preflight-1",
            project_id="proj-agy",
            source="agy",
            machine_id="machine-agy",
            session_type="interactive",
            status="active",
            workspace_path="/tmp/agy-ws",
            tombstoned=False,
        )
        mock_sessions = MagicMock()
        mock_sessions.get.return_value = matching
        mock_sessions.db = session_storage.db
        mock_hook_manager = MagicMock()
        mock_hook_manager.shutdown_async = AsyncMock()
        mock_hook_manager.session_manager = mock_sessions
        mock_hook_manager._session_manager = mock_sessions
        server.app.state.hook_manager = mock_hook_manager

        order: list[str] = []

        def claim(
            _self: object,
            session_id: str,
            owner_token: str | None = None,
        ) -> StartupContextClaim:
            order.append(f"claim:{session_id}")
            return StartupContextClaim("full", 1, owner_token or "owner-1", "claimed")

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.agy.AgyAdapter") as mock_adapter_cls,
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.claim_startup_context",
                claim,
            ),
        ):
            mock_adapter = MagicMock()

            def handle_native(payload: dict[str, Any], _hook_manager: object) -> dict[str, str]:
                order.append("adapter")
                assert payload.get("_gobby_startup_claim") is not None
                return {"decision": "allow"}

            mock_adapter.handle_native.side_effect = handle_native
            mock_adapter_cls.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json=_agy_pre_invocation_envelope(),
                headers={"X-Gobby-Session-Id": "sess-preflight-1"},
            )

        assert response.status_code == 200
        assert order == ["claim:sess-preflight-1", "adapter"]
        body = response.json()
        assert "_gobby_startup_claim" not in body
        assert "owner_token" not in body
        assert "startup_claim_generation" not in body

    def test_mismatching_session_hint_is_rejected_without_claim_or_mutation(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mismatched = SimpleNamespace(
            id="sess-wrong",
            project_id="other-project",
            source="claude",
            machine_id="other-machine",
            session_type="web_chat",
            status="active",
            workspace_path="/other",
            tombstoned=False,
        )
        mock_sessions = MagicMock()
        mock_sessions.get.return_value = mismatched
        mock_hook_manager = MagicMock()
        mock_hook_manager.shutdown_async = AsyncMock()
        mock_hook_manager.session_manager = mock_sessions
        mock_hook_manager._session_manager = mock_sessions
        server.app.state.hook_manager = mock_hook_manager

        claimed_ids: list[str] = []

        def claim(
            _self: object,
            session_id: str,
            owner_token: str | None = None,
        ) -> StartupContextClaim:
            claimed_ids.append(session_id)
            return StartupContextClaim("full", 1, owner_token or "owner-1", "claimed")

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.agy.AgyAdapter") as mock_adapter_cls,
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.claim_startup_context",
                claim,
            ),
        ):
            mock_adapter = MagicMock()
            captured: dict[str, Any] = {}

            def handle_native(
                payload: dict[str, Any],
                _hook_manager: object,
            ) -> dict[str, str]:
                captured.update(payload)
                return {"decision": "allow"}

            mock_adapter.handle_native.side_effect = handle_native
            mock_adapter_cls.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json=_agy_pre_invocation_envelope(),
                headers={"X-Gobby-Session-Id": "sess-wrong"},
            )

        assert response.status_code == 200
        assert "sess-wrong" not in claimed_ids
        mock_sessions.update.assert_not_called()
        mock_adapter.handle_native.assert_called_once()
        hint_error = captured.get("_gobby_session_hint_error")
        assert isinstance(hint_error, str) and hint_error
        assert "sess-wrong" in hint_error
        assert captured.get("_gobby_startup_claim") is None
        assert "_gobby_session_hint_error" not in response.json()
