"""Focused tests for AGY hook dispatch through the unified hooks route."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from gobby.adapters.agy_contract import AGY_FORCE_CONTINUE_LIMIT
from gobby.config.app import DaemonConfig
from gobby.hooks.agent_run_ingress import AgentRunIngressRetryableError
from gobby.hooks.envelope_dedupe import (
    ENVELOPE_ID_HEADER,
    read_envelope_marker,
    release_envelope_processing_claim,
)
from gobby.hooks.runtime_compat import SUPPORTED_HOOK_RESPONSE_CAPABILITY
from gobby.hooks.startup_claim_preflight import StartupClaimLease
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import StartupContextClaim
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

_ADAPTER_TIMEOUT_SECONDS = 0.15


def _processed_dir(gobby_home: Path) -> Path:
    return gobby_home / "hooks" / "inbox" / "processed"


def _without_delivery_receipt(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    body = dict(payload)
    body.pop("_gobby_delivery_receipt", None)
    return body


def _wait_for_marker_status(
    processed_dir: Path,
    envelope_id: str,
    status: str,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    record: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
        if record is not None and record.get("status") == status:
            return record
        time.sleep(0.02)
    raise AssertionError(f"expected marker status {status!r}, last record={record!r}")


def _rewrite_marker(processed_dir: Path, envelope_id: str, **updates: object) -> None:
    record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    assert record is not None
    record.update(updates)
    marker_path = next(processed_dir.glob("*.json"))
    marker_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")


def _agy_pre_invocation_envelope(*, conversation_id: str = "agy-conv-1") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enqueued_at": "2026-06-24T12:00:00Z",
        "critical": False,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "hook_type": "PreInvocation",
        "source": "agy",
        "input_data": {
            "hookEventName": "PreInvocation",
            "conversationId": conversation_id,
            "workspacePaths": ["/tmp/agy-ws"],
            "cwd": "/tmp/agy-ws",
        },
    }


def _agy_post_invocation_envelope(
    *,
    conversation_id: str = "agy-conv-1",
    execution_num: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enqueued_at": "2026-06-24T12:00:00Z",
        "critical": False,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "hook_type": "PostInvocation",
        "source": "agy",
        "input_data": {
            "hookEventName": "PostInvocation",
            "conversationId": conversation_id,
            "executionNum": execution_num,
            "workspacePaths": ["/tmp/agy-ws"],
            "cwd": "/tmp/agy-ws",
        },
    }


def _force_continue_body() -> dict[str, Any]:
    return {
        "terminationBehavior": "force_continue",
        "injectSteps": [{"ephemeralMessage": "stay in the turn"}],
    }


def _budget_receipt(*, envelope_id: str, count: int) -> SimpleNamespace:
    return SimpleNamespace(
        receipt_id=f"receipt-{envelope_id}",
        original_envelope_id=envelope_id,
        delivery_generation=1,
        force_continue_count=count,
    )


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
                "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
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

    def test_durable_preinvocation_stages_startup_claim_without_committing(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        matching = SimpleNamespace(
            id="sess-preflight-2",
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
        receipt = SimpleNamespace(
            receipt_id="receipt-startup-1",
            original_envelope_id="env-startup-1",
            delivery_generation=1,
        )

        def claim(
            _self: object,
            session_id: str,
            owner_token: str | None = None,
        ) -> StartupContextClaim:
            return StartupContextClaim("full", 9, owner_token or "owner-9", "claimed")

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.agy.AgyAdapter") as mock_adapter_cls,
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.claim_startup_context",
                claim,
            ),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.commit_startup_context",
            ) as commit,
            patch(
                "gobby.storage.hook_receipts.prepare_receipt",
                return_value=receipt,
            ) as prepare_receipt,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"decision": "allow"}
            mock_adapter_cls.return_value = mock_adapter
            response = client.post(
                "/api/hooks/execute",
                json=_agy_pre_invocation_envelope(),
                headers={
                    "X-Gobby-Session-Id": "sess-preflight-2",
                    ENVELOPE_ID_HEADER: "env-startup-1",
                },
            )

        assert response.status_code == 200
        commit.assert_not_called()
        prepare_receipt.assert_called_once()
        staged = prepare_receipt.call_args.kwargs.get("staged_payload") or {}
        context = staged.get("startup_context") if isinstance(staged, dict) else None
        assert isinstance(context, dict)
        assert context.get("generation") == 9
        assert context.get("session_id") == "sess-preflight-2"
        assert context.get("owner_token")

    def test_mismatching_session_hint_is_rejected_without_claim_or_mutation(
        self,
        session_storage: SessionManager,
        caplog: pytest.LogCaptureFixture,
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
            caplog.at_level(logging.WARNING, logger="gobby.hooks.startup_claim_preflight"),
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
        assert any(
            record.levelno == logging.WARNING and hint_error in record.getMessage()
            for record in caplog.records
        )

    def test_preflight_timeout_returns_retry_and_invalidates_the_late_lease(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gobby_home = tmp_path / "gobby-home"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        server = _short_timeout_server(session_storage)
        hook_manager = server.app.state.hook_manager
        gate = threading.Event()
        lease = StartupClaimLease("sess-slow", 5, "owner-slow")
        envelope_id = "env-preflight-slow"

        def slow_preflight(_payload: dict[str, Any], _hook_manager: Any) -> StartupClaimLease:
            assert gate.wait(timeout=5)
            return lease

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.hooks.startup_claim_preflight.preflight_agy_startup_claim",
                slow_preflight,
            ),
            patch(
                "gobby.hooks.startup_claim_preflight.invalidate_agy_startup_claim",
            ) as invalidate,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
            ) as run_adapter,
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=_agy_pre_invocation_envelope(),
            )
            assert response.status_code == 503
            assert response.json() == {"status": "retry", "retry_kind": "preflight_timeout"}
            run_adapter.assert_not_awaited()
            invalidate.assert_not_called()
            gate.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not invalidate.call_args_list:
                time.sleep(0.02)
            invalidate.assert_called_once_with(hook_manager, lease)

        marker = read_envelope_marker(envelope_id, processed_dir=_processed_dir(gobby_home))
        assert marker is None or marker.get("status") != "processing"


def _capability_gate_server(session_storage: SessionManager) -> Any:
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_storage,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()
    return server


class TestAgyAdapterTimeoutRetry:
    def test_timeout_without_capability_rejects_before_adapter(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = _capability_gate_server(session_storage)
        envelope = _agy_pre_invocation_envelope()
        envelope.pop("response_capability", None)

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                side_effect=TimeoutError,
            ) as run_adapter,
            patch("gobby.servers.routes.mcp.hooks.finalize_envelope_processed") as finalize,
            patch("gobby.servers.routes.mcp.hooks.mark_envelope_processed") as mark_processed,
            patch("gobby.storage.hook_receipts.prepare_receipt") as prepare_receipt,
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: "env-agy-timeout-legacy"},
                json=envelope,
            )

        assert response.status_code == 200
        body = response.json()
        assert body.get("retry_kind") is None
        assert body.get("status") != "retry"
        run_adapter.assert_not_called()
        finalize.assert_not_called()
        mark_processed.assert_not_called()
        prepare_receipt.assert_not_called()

    def test_timeout_with_capability_returns_503_adapter_timeout(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = MagicMock()
        server.app.state.hook_manager.shutdown_async = AsyncMock()
        envelope = _agy_pre_invocation_envelope()
        envelope["response_capability"] = SUPPORTED_HOOK_RESPONSE_CAPABILITY

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                side_effect=TimeoutError,
            ),
            patch("gobby.servers.routes.mcp.hooks.mark_envelope_processed") as mark_processed,
            patch(
                "gobby.servers.routes.mcp.hooks.release_envelope_processing_claim",
                return_value=True,
            ) as release,
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: "env-agy-timeout"},
                json=envelope,
            )

        assert response.status_code == 503
        assert response.json() == {
            "status": "retry",
            "retry_kind": "adapter_timeout",
        }
        mark_processed.assert_not_called()
        release.assert_called_once_with("env-agy-timeout")

    def test_ingress_retry_includes_retry_kind_discriminator(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = MagicMock()
        server.app.state.hook_manager.shutdown_async = AsyncMock()
        envelope = _agy_pre_invocation_envelope()
        envelope["response_capability"] = SUPPORTED_HOOK_RESPONSE_CAPABILITY
        retryable = AgentRunIngressRetryableError(
            session_id="agy-child",
            expected_run_id="run-1",
            reason="run is not durable yet",
        )

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                side_effect=retryable,
            ),
            patch("gobby.servers.routes.mcp.hooks.mark_envelope_processed") as mark_processed,
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: "env-agy-ingress"},
                json=envelope,
            )

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "retry"
        assert body["retry_kind"] == "ingress_backpressure"
        assert body["reason"] == "agent_run_identity_pending"
        mark_processed.assert_not_called()


def _short_timeout_server(session_storage: SessionManager) -> Any:
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_storage,
        config=DaemonConfig(),
    )
    assert server.config is not None
    server.config.workflow.timeout = 0.05
    server.config.hooks.adapter_timeout = _ADAPTER_TIMEOUT_SECONDS
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()
    return server


class TestAgyAdapterTimeoutFencing:
    def test_timeout_keeps_claim_until_completed_worker_finalizes(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.adapters.agy import AgyAdapter

        gobby_home = tmp_path / "gobby-home"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        envelope_id = "env-agy-fence-complete"
        envelope = _agy_pre_invocation_envelope()
        envelope["response_capability"] = SUPPORTED_HOOK_RESPONSE_CAPABILITY
        started = threading.Event()
        gate = threading.Event()
        effects = {"session": 0, "rule": 0, "pending": 0, "activity": 0}

        def handle_native(
            _self: object,
            _payload: dict[str, Any],
            _hook_manager: object,
        ) -> dict[str, Any]:
            effects["session"] += 1
            effects["rule"] += 1
            effects["pending"] += 1
            effects["activity"] += 1
            started.set()
            assert gate.wait(timeout=5)
            return {"continue": True, "decision": "allow"}

        server = _short_timeout_server(session_storage)
        processed_dir = _processed_dir(gobby_home)
        with (
            TestClient(server.app) as client,
            patch.object(AgyAdapter, "handle_native", handle_native),
        ):
            timeout_response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=envelope,
            )
            assert started.wait(timeout=2)
            assert timeout_response.status_code == 503
            assert timeout_response.json() == {
                "status": "retry",
                "retry_kind": "adapter_timeout",
            }
            in_flight = read_envelope_marker(envelope_id, processed_dir=processed_dir)
            assert in_flight is not None
            assert in_flight.get("status") == "processing"

            replay = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=envelope,
            )
            assert replay.status_code == 409
            assert replay.json()["status"] == "processing"
            assert effects == {"session": 1, "rule": 1, "pending": 1, "activity": 1}

            gate.set()
            processed = _wait_for_marker_status(processed_dir, envelope_id, "processed")
            assert _without_delivery_receipt(processed.get("response")) == {
                "continue": True,
                "decision": "allow",
            }

            stored = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=envelope,
            )
            assert stored.status_code == 200
            assert _without_delivery_receipt(stored.json()) == {
                "continue": True,
                "decision": "allow",
            }
            assert effects == {"session": 1, "rule": 1, "pending": 1, "activity": 1}

    def test_failed_worker_after_timeout_releases_claim_for_replay(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.adapters.agy import AgyAdapter

        gobby_home = tmp_path / "gobby-home"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        envelope_id = "env-agy-fence-fail"
        envelope = _agy_pre_invocation_envelope()
        envelope["response_capability"] = SUPPORTED_HOOK_RESPONSE_CAPABILITY
        started = threading.Event()
        gate = threading.Event()

        def handle_native(
            _self: object,
            _payload: dict[str, Any],
            _hook_manager: object,
        ) -> dict[str, Any]:
            started.set()
            assert gate.wait(timeout=5)
            raise RuntimeError("adapter failed")

        server = _short_timeout_server(session_storage)
        processed_dir = _processed_dir(gobby_home)
        with (
            TestClient(server.app) as client,
            patch.object(AgyAdapter, "handle_native", handle_native),
        ):
            timeout_response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=envelope,
            )
            assert started.wait(timeout=2)
            assert timeout_response.status_code == 503
            assert read_envelope_marker(envelope_id, processed_dir=processed_dir) is not None

            released = threading.Event()
            real_release = release_envelope_processing_claim

            def _track_release(*args: Any, **kwargs: Any) -> bool:
                result = real_release(*args, **kwargs)
                released.set()
                return result

            with patch(
                "gobby.hooks.adapter_execution.release_envelope_processing_claim",
                _track_release,
            ):
                gate.set()
                assert released.wait(timeout=2)
            assert read_envelope_marker(envelope_id, processed_dir=processed_dir) is None

    def test_late_worker_output_after_lost_lease_is_discarded(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.adapters.agy import AgyAdapter

        gobby_home = tmp_path / "gobby-home"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        envelope_id = "env-agy-fence-lost"
        envelope = _agy_pre_invocation_envelope()
        envelope["response_capability"] = SUPPORTED_HOOK_RESPONSE_CAPABILITY
        started = threading.Event()
        gate = threading.Event()

        def handle_native(
            _self: object,
            _payload: dict[str, Any],
            _hook_manager: object,
        ) -> dict[str, Any]:
            started.set()
            assert gate.wait(timeout=5)
            return {"continue": True, "decision": "allow", "source": "late-worker"}

        server = _short_timeout_server(session_storage)
        processed_dir = _processed_dir(gobby_home)
        with (
            TestClient(server.app) as client,
            patch.object(AgyAdapter, "handle_native", handle_native),
        ):
            timeout_response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=envelope,
            )
            assert started.wait(timeout=2)
            assert timeout_response.status_code == 503
            _rewrite_marker(processed_dir, envelope_id, owner_token="thief-token")
            finalized = threading.Event()

            def _track_finalize(*args: Any, **kwargs: Any) -> bool:
                from gobby.hooks.envelope_dedupe import finalize_envelope_processed as real

                try:
                    return real(*args, **kwargs)
                finally:
                    finalized.set()

            with patch(
                "gobby.hooks.adapter_execution.finalize_envelope_processed",
                _track_finalize,
            ):
                gate.set()
                assert finalized.wait(timeout=2)
            record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
            assert record is not None
            assert record.get("status") == "processing"
            assert record.get("owner_token") == "thief-token"
            assert record.get("response") is None

    def test_timeout_invalidates_startup_claim_and_does_not_commit(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.adapters.agy import AgyAdapter

        gobby_home = tmp_path / "gobby-home"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        envelope_id = "env-agy-fence-startup"
        envelope = _agy_pre_invocation_envelope()
        envelope["response_capability"] = SUPPORTED_HOOK_RESPONSE_CAPABILITY
        started = threading.Event()
        gate = threading.Event()
        matching = SimpleNamespace(
            id="sess-fence-1",
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
        server = _short_timeout_server(session_storage)
        server.app.state.hook_manager.session_manager = mock_sessions
        server.app.state.hook_manager._session_manager = mock_sessions

        def handle_native(
            _self: object,
            _payload: dict[str, Any],
            _hook_manager: object,
        ) -> dict[str, Any]:
            started.set()
            assert gate.wait(timeout=5)
            return {"continue": True, "decision": "allow"}

        def claim(
            _self: object,
            session_id: str,
            owner_token: str | None = None,
        ) -> StartupContextClaim:
            return StartupContextClaim("full", 7, owner_token or "owner-1", "claimed")

        with (
            TestClient(server.app) as client,
            patch.object(AgyAdapter, "handle_native", handle_native),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.claim_startup_context",
                claim,
            ),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.commit_startup_context",
            ) as commit,
            patch(
                "gobby.servers.routes.mcp.hooks.invalidate_agy_startup_claim",
            ) as invalidate,
        ):
            timeout_response = client.post(
                "/api/hooks/execute",
                headers={
                    ENVELOPE_ID_HEADER: envelope_id,
                    "X-Gobby-Session-Id": "sess-fence-1",
                },
                json=envelope,
            )
            assert started.wait(timeout=2)
            assert timeout_response.status_code == 503
            invalidate.assert_called()
            gate.set()
            _wait_for_marker_status(_processed_dir(gobby_home), envelope_id, "processed")
            commit.assert_not_called()

    @pytest.mark.parametrize(
        ("source", "hook_type", "adapter_path", "critical"),
        [
            ("claude", "session-start", "gobby.adapters.claude_code.ClaudeCodeAdapter", True),
            ("droid", "PreToolUse", "gobby.adapters.droid.DroidAdapter", False),
        ],
    )
    def test_non_agy_adapter_timeout_uses_the_same_fencing(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        source: str,
        hook_type: str,
        adapter_path: str,
        critical: bool,
    ) -> None:
        gobby_home = tmp_path / "gobby-home"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        envelope_id = f"env-{source}-fence"
        envelope = {
            "schema_version": 1,
            "enqueued_at": "2026-06-24T12:00:00Z",
            "critical": critical,
            "hook_type": hook_type,
            "source": source,
            "input_data": {"session_id": f"{source}-1", "cwd": "/tmp"},
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        }
        started = threading.Event()
        gate = threading.Event()

        def handle_native(
            _self: object,
            _payload: dict[str, Any],
            _hook_manager: object,
        ) -> dict[str, Any]:
            started.set()
            assert gate.wait(timeout=5)
            return {"continue": True}

        server = _short_timeout_server(session_storage)
        processed_dir = _processed_dir(gobby_home)
        with (
            TestClient(server.app) as client,
            patch(f"{adapter_path}.handle_native", handle_native),
        ):
            timeout_response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=envelope,
            )
            assert started.wait(timeout=2)
            assert timeout_response.status_code == 503
            assert timeout_response.json()["retry_kind"] == "adapter_timeout"
            replay = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=envelope,
            )
            assert replay.status_code == 409
            gate.set()
            processed = _wait_for_marker_status(processed_dir, envelope_id, "processed")
            assert _without_delivery_receipt(processed.get("response")) == {"continue": True}


class TestHookResponseCapabilityGate:
    def test_direct_post_missing_capability_rejects_before_adapter(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = _capability_gate_server(session_storage)
        envelope = _agy_pre_invocation_envelope()
        envelope.pop("response_capability", None)

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value={"decision": "allow"},
            ) as run_adapter,
        ):
            response = client.post("/api/hooks/execute", json=envelope)

        assert response.status_code == 200
        assert response.json().get("retry_kind") is None
        run_adapter.assert_not_called()

    def test_direct_post_advertised_below_floor_rejects_before_adapter(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = _capability_gate_server(session_storage)
        envelope = _agy_pre_invocation_envelope()
        envelope["response_capability"] = "hook-response.v0"

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value={"decision": "allow"},
            ) as run_adapter,
        ):
            response = client.post("/api/hooks/execute", json=envelope)

        assert response.status_code == 200
        run_adapter.assert_not_called()

    def test_detached_post_missing_capability_rejects_before_adapter(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = _capability_gate_server(session_storage)
        envelope = _agy_pre_invocation_envelope()
        envelope.pop("response_capability", None)

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value={"decision": "allow"},
            ) as run_adapter,
            patch("gobby.servers.routes.mcp.hooks.claim_envelope_processing") as claim,
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: "env-agy-detached-legacy"},
                json=envelope,
            )

        assert response.status_code == 200
        run_adapter.assert_not_called()
        claim.assert_not_called()

    def test_compatible_stamp_does_not_lift_below_floor_request(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.hooks.runtime_compat import (
            SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
            GhookRuntimeDiagnostic,
            GhookRuntimeState,
        )

        stamp = tmp_path / ".ghook-runtime.json"
        monkeypatch.setattr(
            "gobby.hooks.runtime_compat.read_ghook_runtime_diagnostic",
            lambda _path=None: GhookRuntimeDiagnostic(
                state=GhookRuntimeState.COMPATIBLE,
                stamp_path=str(stamp),
                detail="reinstalled",
                schema_version=SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
                ghook_version="0.7.3",
                response_capability=SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            ),
        )
        server = _capability_gate_server(session_storage)
        envelope = _agy_pre_invocation_envelope()
        envelope.pop("response_capability", None)

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value={"decision": "allow"},
            ) as run_adapter,
        ):
            response = client.post("/api/hooks/execute", json=envelope)

        assert response.status_code == 200
        run_adapter.assert_not_called()

    def test_capable_request_still_runs_adapter(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = _capability_gate_server(session_storage)
        envelope = _agy_pre_invocation_envelope()
        envelope["response_capability"] = SUPPORTED_HOOK_RESPONSE_CAPABILITY

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value={"decision": "allow"},
            ) as run_adapter,
        ):
            response = client.post("/api/hooks/execute", json=envelope)

        assert response.status_code == 200
        assert response.json() == {"decision": "allow"}
        run_adapter.assert_awaited_once()


def _delivery_receipt_server(session_storage: SessionManager) -> Any:
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_storage,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()
    return server


class TestExecuteHookDeliveryReceipt:
    def test_durable_envelope_attaches_delivery_receipt(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)
        envelope = _agy_pre_invocation_envelope()
        receipt = SimpleNamespace(
            receipt_id="receipt-durable-1",
            original_envelope_id="env-durable-1",
            delivery_generation=1,
        )

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value={"decision": "allow"},
            ),
            patch(
                "gobby.storage.hook_receipts.prepare_receipt",
                return_value=receipt,
            ) as prepare_receipt,
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: "env-durable-1"},
                json=envelope,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["decision"] == "allow"
        assert body["_gobby_delivery_receipt"] == {
            "receipt_id": "receipt-durable-1",
            "original_envelope_id": "env-durable-1",
            "delivery_generation": 1,
        }
        prepare_receipt.assert_called_once()
        kwargs = prepare_receipt.call_args.kwargs
        assert kwargs["envelope_id"] == "env-durable-1"
        assert kwargs["session_id"]

    def test_durable_envelope_stages_pending_message_effects(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)
        envelope = _agy_pre_invocation_envelope()
        staged = {
            "pending_message_ids": ["msg-lossless"],
            "pending_message_session_id": "recipient-session",
        }
        receipt = SimpleNamespace(
            receipt_id="receipt-staged-1",
            original_envelope_id="env-staged-1",
            delivery_generation=1,
        )

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value={"decision": "allow", "_gobby_staged_effects": staged},
            ),
            patch(
                "gobby.storage.hook_receipts.prepare_receipt",
                return_value=receipt,
            ) as prepare_receipt,
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: "env-staged-1"},
                json=envelope,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["decision"] == "allow"
        assert "_gobby_staged_effects" not in body
        assert body["_gobby_delivery_receipt"]["receipt_id"] == "receipt-staged-1"
        prepare_receipt.assert_called_once()
        assert prepare_receipt.call_args.kwargs["staged_payload"] == staged

    def test_identity_less_strips_staged_effects_without_preparing(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)
        envelope = _agy_pre_invocation_envelope()
        staged = {
            "pending_message_ids": ["msg-lossless"],
            "pending_message_session_id": "recipient-session",
        }

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value={"decision": "allow", "_gobby_staged_effects": staged},
            ),
            patch("gobby.storage.hook_receipts.prepare_receipt") as prepare_receipt,
        ):
            response = client.post("/api/hooks/execute", json=envelope)

        assert response.status_code == 200
        assert response.json() == {"decision": "allow"}
        prepare_receipt.assert_not_called()

    def test_identity_less_direct_post_does_not_prepare_or_attach(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)
        envelope = _agy_pre_invocation_envelope()

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value={"decision": "allow"},
            ),
            patch("gobby.storage.hook_receipts.prepare_receipt") as prepare_receipt,
        ):
            response = client.post("/api/hooks/execute", json=envelope)

        assert response.status_code == 200
        assert response.json() == {"decision": "allow"}
        prepare_receipt.assert_not_called()

    def test_prepare_failure_still_emits_adapter_result(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)
        envelope = _agy_pre_invocation_envelope()

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value={"decision": "allow"},
            ),
            patch(
                "gobby.storage.hook_receipts.prepare_receipt",
                side_effect=RuntimeError("receipt store down"),
            ) as prepare_receipt,
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: "env-durable-fail"},
                json=envelope,
            )

        assert response.status_code == 200
        assert response.json() == {"decision": "allow"}
        prepare_receipt.assert_called_once()


class TestExecuteHookReceiptRedelivery:
    def test_next_live_hook_carries_the_lost_delivery_and_stale_ack_is_a_noop(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.hook_receipts import acknowledge_receipt, release_receipt

        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)
        db = session_storage.db
        session_id = str(uuid4())
        first_body = {
            "decision": "allow",
            "_gobby_staged_effects": {
                "session_id": session_id,
                "session_variables": {"first": True},
            },
        }
        second_body = {
            "decision": "allow",
            "_gobby_staged_effects": {
                "session_id": session_id,
                "session_variables": {"second": True},
            },
        }

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                side_effect=[first_body, second_body],
            ),
        ):
            first = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: "env-carry-1", "X-Gobby-Session-Id": session_id},
                json=_agy_post_invocation_envelope(),
            )
            assert first.status_code == 200
            first_receipt = first.json()["_gobby_delivery_receipt"]
            assert first_receipt["delivery_generation"] == 1
            # Transport loss: the emitted response never reached the hook process.
            assert release_receipt(db, receipt_id=first_receipt["receipt_id"]) is not None

            second = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: "env-carry-2", "X-Gobby-Session-Id": session_id},
                json=_agy_post_invocation_envelope(),
            )

        assert second.status_code == 200
        assert "_gobby_staged_effects" not in second.json()
        second_receipt = second.json()["_gobby_delivery_receipt"]
        assert second_receipt["receipt_id"] == first_receipt["receipt_id"]
        assert second_receipt["original_envelope_id"] == "env-carry-1"
        assert second_receipt["delivery_generation"] == 2

        stale = acknowledge_receipt(
            db, receipt_id=first_receipt["receipt_id"], delivery_generation=1
        )
        assert stale is None
        row = db.fetchone(
            "SELECT state, current_envelope_id FROM hook_receipt_effects WHERE receipt_id = %s",
            (first_receipt["receipt_id"],),
        )
        assert row is not None
        assert row["state"] == "prepared"
        assert row["current_envelope_id"] == "env-carry-2"

        committed = acknowledge_receipt(
            db, receipt_id=first_receipt["receipt_id"], delivery_generation=2
        )
        assert committed is not None
        assert committed.state == "acknowledged"
        assert committed.staged_payload["session_variables"] == {"first": True, "second": True}
        count = db.fetchone(
            "SELECT count(*) AS n FROM hook_receipt_effects WHERE session_id = %s",
            (session_id,),
        )
        assert count is not None and int(count["n"]) == 1

    @pytest.mark.asyncio
    async def test_emission_failure_releases_the_receipt(
        self,
        session_storage: SessionManager,
    ) -> None:
        from gobby.hooks.receipt_redelivery import receipt_guarded_response
        from gobby.storage.hook_receipts import prepare_receipt

        db = session_storage.db
        receipt = prepare_receipt(db, session_id=str(uuid4()), envelope_id="env-emit-fail")
        body = {
            "decision": "allow",
            "_gobby_delivery_receipt": {
                "receipt_id": receipt.receipt_id,
                "original_envelope_id": "env-emit-fail",
                "delivery_generation": 1,
            },
        }
        response = receipt_guarded_response(body, db=db)
        scope = {"type": "http", "method": "POST", "headers": []}

        async def receive() -> dict[str, Any]:
            return {"type": "http.request"}

        async def failing_send(_message: dict[str, Any]) -> None:
            raise ConnectionResetError("client went away")

        with pytest.raises(ConnectionResetError):
            await response(scope, receive, failing_send)

        row = db.fetchone(
            "SELECT state FROM hook_receipt_effects WHERE receipt_id = %s",
            (receipt.receipt_id,),
        )
        assert row is not None
        assert row["state"] == "released"

    @pytest.mark.asyncio
    async def test_successful_emission_keeps_the_receipt_prepared(
        self,
        session_storage: SessionManager,
    ) -> None:
        from gobby.hooks.receipt_redelivery import receipt_guarded_response
        from gobby.storage.hook_receipts import prepare_receipt

        db = session_storage.db
        receipt = prepare_receipt(db, session_id=str(uuid4()), envelope_id="env-emit-ok")
        body = {
            "decision": "allow",
            "_gobby_delivery_receipt": {
                "receipt_id": receipt.receipt_id,
                "original_envelope_id": "env-emit-ok",
                "delivery_generation": 1,
            },
        }
        response = receipt_guarded_response(body, db=db)
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await response({"type": "http", "method": "POST", "headers": []}, receive, send)

        assert any(message.get("type") == "http.response.body" for message in sent)
        row = db.fetchone(
            "SELECT state FROM hook_receipt_effects WHERE receipt_id = %s",
            (receipt.receipt_id,),
        )
        assert row is not None
        assert row["state"] == "prepared"


class TestAgyForceContinueBudget:
    def test_identity_less_post_invocation_strips_force_continue(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value=_force_continue_body(),
            ),
            patch("gobby.storage.hook_receipts.prepare_receipt") as prepare_receipt,
        ):
            response = client.post(
                "/api/hooks/execute",
                json=_agy_post_invocation_envelope(execution_num=3),
            )

        assert response.status_code == 200
        body = response.json()
        assert "terminationBehavior" not in body
        assert body["injectSteps"] == [{"ephemeralMessage": "stay in the turn"}]
        prepare_receipt.assert_not_called()

    def test_durable_post_invocation_prepares_with_execution_num(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)
        envelope_id = "env-fc-durable"

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value=_force_continue_body(),
            ),
            patch(
                "gobby.storage.hook_receipts.prepare_receipt",
                return_value=_budget_receipt(envelope_id=envelope_id, count=1),
            ) as prepare_receipt,
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=_agy_post_invocation_envelope(execution_num=3),
            )

        assert response.status_code == 200
        body = response.json()
        assert body["terminationBehavior"] == "force_continue"
        assert prepare_receipt.call_args.kwargs.get("force_continue_execution_num") == 3

    def test_limit_th_emission_is_still_force_continue(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)
        envelope_id = "env-fc-limit"

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value=_force_continue_body(),
            ),
            patch(
                "gobby.storage.hook_receipts.prepare_receipt",
                return_value=_budget_receipt(
                    envelope_id=envelope_id,
                    count=AGY_FORCE_CONTINUE_LIMIT,
                ),
            ),
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=_agy_post_invocation_envelope(),
            )

        assert response.status_code == 200
        assert response.json()["terminationBehavior"] == "force_continue"

    def test_over_limit_deny_is_ephemeral_message_only(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)
        envelope_id = "env-fc-over"

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value=_force_continue_body(),
            ),
            patch(
                "gobby.storage.hook_receipts.prepare_receipt",
                return_value=_budget_receipt(
                    envelope_id=envelope_id,
                    count=AGY_FORCE_CONTINUE_LIMIT + 1,
                ),
            ),
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: envelope_id},
                json=_agy_post_invocation_envelope(),
            )

        assert response.status_code == 200
        body = response.json()
        assert "terminationBehavior" not in body
        assert body["injectSteps"] == [{"ephemeralMessage": "stay in the turn"}]

    def test_concurrent_posts_serialize_and_downgrade_the_loser(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)
        counter = {"n": AGY_FORCE_CONTINUE_LIMIT - 1}
        lock = threading.Lock()

        def prepare_receipt(*_args: object, **kwargs: object) -> SimpleNamespace:
            envelope_id = str(kwargs["envelope_id"])
            with lock:
                counter["n"] += 1
                count = counter["n"]
            return _budget_receipt(envelope_id=envelope_id, count=count)

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value=_force_continue_body(),
            ),
            patch(
                "gobby.storage.hook_receipts.prepare_receipt",
                side_effect=prepare_receipt,
            ),
        ):
            results: list[dict[str, Any]] = []

            def _post(envelope_id: str) -> None:
                response = client.post(
                    "/api/hooks/execute",
                    headers={ENVELOPE_ID_HEADER: envelope_id},
                    json=_agy_post_invocation_envelope(),
                )
                assert response.status_code == 200
                results.append(response.json())

            workers = [
                threading.Thread(target=_post, args=(f"env-fc-race-{index}",)) for index in range(2)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

        behaviors = [body.get("terminationBehavior") for body in results]
        assert behaviors.count("force_continue") == 1
        assert behaviors.count(None) == 1
        assert all(
            body["injectSteps"] == [{"ephemeralMessage": "stay in the turn"}] for body in results
        )

    def test_prepare_failure_does_not_emit_force_continue(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
        server = _delivery_receipt_server(session_storage)

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                return_value=_force_continue_body(),
            ),
            patch(
                "gobby.storage.hook_receipts.prepare_receipt",
                side_effect=RuntimeError("receipt store down"),
            ),
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={ENVELOPE_ID_HEADER: "env-fc-fail"},
                json=_agy_post_invocation_envelope(),
            )

        assert response.status_code == 200
        body = response.json()
        assert "terminationBehavior" not in body
        assert body["injectSteps"] == [{"ephemeralMessage": "stay in the turn"}]
