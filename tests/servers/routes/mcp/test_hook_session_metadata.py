"""Regression tests for hook ingress platform session metadata."""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.hooks.envelope_dedupe import (
    ENVELOPE_ID_HEADER,
    ENVELOPE_REPLAY_GRACE_SECONDS,
    claim_envelope_processing,
    envelope_terminal_response,
    is_envelope_processed,
    mark_envelope_processed,
    read_envelope_marker,
)
from gobby.hooks.runtime_compat import SUPPORTED_HOOK_RESPONSE_CAPABILITY
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def _post_claude_hook(temp_db: HubDatabase, payload: dict, headers: dict | None = None) -> dict:
    session_manager = SessionManager(temp_db)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        adapter = MagicMock()
        adapter.handle_native.return_value = {"continue": True}
        adapter_cls.return_value = adapter

        envelope = {
            "schema_version": 1,
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            "enqueued_at": "2026-04-16T12:00:00Z",
            "critical": False,
            "input_data": {},
            **payload,
        }
        response = client.post("/api/hooks/execute", json=envelope, headers=headers or {})

    assert response.status_code == 200
    return adapter.handle_native.call_args.args[0]


def test_real_session_header_is_passed_to_adapter_payload(temp_db: HubDatabase) -> None:
    adapter_payload = _post_claude_hook(
        temp_db,
        {
            "hook_type": "session-start",
            "source": "claude",
            "input_data": {"session_id": "claude-external"},
        },
        headers={"X-Gobby-Session-Id": "platform-session"},
    )

    assert adapter_payload["_platform_session_id"] == "platform-session"
    assert adapter_payload["input_data"]["session_id"] == "claude-external"


def test_envelope_id_marks_processed_and_skips_duplicate(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    session_manager = SessionManager(temp_db)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    envelope = {
        "schema_version": 1,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "source": "claude",
        "input_data": {},
    }

    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        adapter = MagicMock()
        adapter.handle_native.return_value = {"continue": True, "decision": "approve"}
        adapter_cls.return_value = adapter

        first_response = client.post(
            "/api/hooks/execute",
            json=envelope,
            headers={ENVELOPE_ID_HEADER: "n-0000000000001-abcd"},
        )
        second_response = client.post(
            "/api/hooks/execute",
            json=envelope,
            headers={ENVELOPE_ID_HEADER: "n-0000000000001-abcd"},
        )

    processed_dir = tmp_path / "gobby-home" / "hooks" / "inbox" / "processed"
    assert first_response.status_code == 200
    assert is_envelope_processed("n-0000000000001-abcd", processed_dir=processed_dir)
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()
    adapter.handle_native.assert_called_once()


def test_envelope_id_active_processing_duplicate_returns_conflict(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    session_manager = SessionManager(temp_db)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    envelope_id = "n-0000000000001-abcd"
    processed_dir = tmp_path / "gobby-home" / "hooks" / "inbox" / "processed"
    claim_envelope_processing(envelope_id, processed_dir=processed_dir)
    envelope = {
        "schema_version": 1,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "source": "claude",
        "input_data": {},
    }

    with (
        caplog.at_level("DEBUG", logger="gobby.servers.routes.mcp.hooks"),
        TestClient(server.app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        response = client.post(
            "/api/hooks/execute",
            json=envelope,
            headers={ENVELOPE_ID_HEADER: envelope_id},
        )

    hook_records = [
        record for record in caplog.records if record.name == "gobby.servers.routes.mcp.hooks"
    ]
    assert response.status_code == 409
    assert response.json() == {
        "status": "processing",
        "reason": "duplicate envelope already processing",
    }
    adapter_cls.assert_not_called()
    assert "duplicate envelope already processing" in caplog.text
    assert all(record.levelno < logging.WARNING for record in hook_records)


def test_envelope_id_malformed_marker_returns_clear_conflict(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    session_manager = SessionManager(temp_db)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    envelope_id = "n-0000000000004-abcd"
    processed_dir = tmp_path / "gobby-home" / "hooks" / "inbox" / "processed"
    claim_envelope_processing(envelope_id, processed_dir=processed_dir)
    marker_path = next(processed_dir.glob("*.json"))
    marker_path.write_text("[]\n", encoding="utf-8")
    envelope = {
        "schema_version": 1,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "source": "claude",
        "input_data": {},
    }

    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        response = client.post(
            "/api/hooks/execute",
            json=envelope,
            headers={ENVELOPE_ID_HEADER: envelope_id},
        )

    assert response.status_code == 409
    assert response.json() == {
        "status": "malformed_marker",
        "reason": "duplicate envelope marker malformed",
    }
    adapter_cls.assert_not_called()


def test_envelope_id_replays_terminal_denial(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    session_manager = SessionManager(temp_db)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    envelope = {
        "schema_version": 1,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": True,
        "hook_type": "PreToolUse",
        "source": "claude",
        "input_data": {},
    }
    terminal_response = {"continue": False, "decision": "block", "reason": "commit required"}

    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        adapter = MagicMock()
        adapter.handle_native.return_value = terminal_response
        adapter_cls.return_value = adapter

        first_response = client.post(
            "/api/hooks/execute",
            json=envelope,
            headers={ENVELOPE_ID_HEADER: "n-0000000000002-abcd"},
        )
        second_response = client.post(
            "/api/hooks/execute",
            json=envelope,
            headers={ENVELOPE_ID_HEADER: "n-0000000000002-abcd"},
        )

    processed_dir = tmp_path / "gobby-home" / "hooks" / "inbox" / "processed"
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json() == terminal_response
    assert second_response.json() == terminal_response
    assert (
        envelope_terminal_response("n-0000000000002-abcd", processed_dir=processed_dir)
        == terminal_response
    )
    adapter.handle_native.assert_called_once()


def test_aged_stop_block_is_reevaluated_instead_of_replayed(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    session_manager = SessionManager(temp_db)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    envelope = {
        "schema_version": 1,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": True,
        "hook_type": "Stop",
        "source": "claude",
        "input_data": {},
    }
    terminal_response = {
        "continue": True,
        "decision": "block",
        "reason": "Rule enforced by Gobby: [block-terminal-validation-failure]\nfix it",
    }
    allow_response = {"continue": True, "decision": "allow"}
    envelope_id = "n-0000000000003-stop"

    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        adapter = MagicMock()
        adapter.handle_native.side_effect = [terminal_response, allow_response]
        adapter_cls.return_value = adapter

        first_response = client.post(
            "/api/hooks/execute",
            json=envelope,
            headers={ENVELOPE_ID_HEADER: envelope_id},
        )
        processed_dir = tmp_path / "gobby-home" / "hooks" / "inbox" / "processed"
        record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
        assert record is not None
        record["processed_at"] = (
            datetime.now(UTC) - timedelta(seconds=ENVELOPE_REPLAY_GRACE_SECONDS + 5)
        ).isoformat()
        marker = next(processed_dir.glob("*.json"))
        marker.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

        second_response = client.post(
            "/api/hooks/execute",
            json=envelope,
            headers={ENVELOPE_ID_HEADER: envelope_id},
        )

    assert first_response.status_code == 200
    assert first_response.json() == terminal_response
    assert second_response.status_code == 200
    assert second_response.json() == allow_response
    assert adapter.handle_native.call_count == 2


def test_envelope_id_processed_marker_without_response_returns_conflict(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    session_manager = SessionManager(temp_db)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    envelope_id = "n-0000000000003-abcd"
    processed_dir = tmp_path / "gobby-home" / "hooks" / "inbox" / "processed"
    mark_envelope_processed(envelope_id, processed_dir=processed_dir)
    envelope = {
        "schema_version": 1,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "source": "claude",
        "input_data": {},
    }

    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        response = client.post(
            "/api/hooks/execute",
            json=envelope,
            headers={ENVELOPE_ID_HEADER: envelope_id},
        )

    assert response.status_code == 409
    assert response.json() == {
        "status": "processed",
        "reason": "duplicate envelope previously processed",
    }
    adapter_cls.assert_not_called()


def test_envelope_headers_cannot_override_real_session_header(temp_db: HubDatabase) -> None:
    adapter_payload = _post_claude_hook(
        temp_db,
        {
            "schema_version": 1,
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            "headers": {"X-Gobby-Session-Id": "embedded-session"},
            "hook_type": "session-start",
            "source": "claude",
            "input_data": {"session_id": "claude-external"},
        },
        headers={"X-Gobby-Session-Id": "real-session"},
    )

    assert adapter_payload["_platform_session_id"] == "real-session"


def test_embedded_envelope_headers_are_ignored_without_real_header(
    temp_db: HubDatabase,
) -> None:
    adapter_payload = _post_claude_hook(
        temp_db,
        {
            "schema_version": 1,
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            "headers": {"X-Gobby-Session-Id": "embedded-session"},
            "hook_type": "session-start",
            "source": "claude",
            "input_data": {"session_id": "claude-external"},
        },
    )

    assert "_platform_session_id" not in adapter_payload


@pytest.mark.parametrize("hook_type", ["Stop", "sToP"])
def test_codex_stop_hook_timeout_blocks_fail_safe(
    temp_db: HubDatabase,
    hook_type: str,
) -> None:
    session_manager = SessionManager(temp_db)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.codex_impl.hooks_adapter.CodexHooksAdapter") as adapter_cls,
        patch(
            "gobby.servers.routes.mcp.hooks._run_adapter_hook",
            new=AsyncMock(side_effect=TimeoutError()),
        ),
    ):
        adapter = MagicMock()
        adapter.translate_from_hook_response.return_value = {
            "continue": False,
            "decision": "block",
            "reason": "timed out",
        }
        adapter_cls.return_value = adapter

        response = client.post(
            "/api/hooks/execute",
            json={
                "schema_version": 1,
                "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
                "enqueued_at": "2026-04-16T12:00:00Z",
                "critical": True,
                "hook_type": hook_type,
                "source": "codex",
                "input_data": {},
            },
        )

        assert response.status_code == 503
        assert response.json() == {"status": "retry", "retry_kind": "adapter_timeout"}
        adapter.translate_from_hook_response.assert_not_called()
