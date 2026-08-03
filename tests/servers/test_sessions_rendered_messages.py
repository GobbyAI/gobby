"""Tests for rendered messages endpoint in session routes."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcript_reader import TranscriptReader
from gobby.sessions.transcript_renderer import render_transcript
from gobby.sessions.transcript_window import WindowResult
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def session_storage(temp_db: HubDatabase) -> SessionManager:
    """Create session storage."""
    return SessionManager(temp_db)


@pytest.fixture
def project_storage(temp_db: HubDatabase) -> LocalProjectManager:
    """Create project storage."""
    return LocalProjectManager(temp_db)


@pytest.fixture
def test_project(project_storage: LocalProjectManager, temp_dir: Path) -> dict[str, Any]:
    """Create a test project with project.json file."""
    project = project_storage.create(name="test-project", repo_path=str(temp_dir))

    gobby_dir = temp_dir / ".gobby"
    gobby_dir.mkdir(exist_ok=True)
    (gobby_dir / "project.json").write_text(f'{{"id": "{project.id}", "name": "test-project"}}')

    return project.to_dict()


class TestGetMessagesRendered:
    """Tests for sessions_get_messages with format=rendered."""

    def test_get_messages_rendered_default(
        self,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test that format=rendered is the default."""
        session = session_storage.register(
            external_id="rendered-test",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        # Mock transcript_reader
        mock_rendered = MagicMock()
        mock_rendered.to_dict.return_value = {"content_blocks": [{"type": "text", "text": "hello"}]}

        mock_reader = AsyncMock()
        mock_reader.get_rendered_window = AsyncMock(
            return_value=WindowResult(
                groups=[mock_rendered],
                returned_count=1,
                total_groups=1,
                parsed_message_count=3,
            )
        )

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
            transcript_reader=mock_reader,
        )

        test_client = TestClient(server.app)
        response = test_client.get(f"/api/sessions/{session.id}/messages")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["format"] == "rendered"
        assert data["order"] == "head"
        assert len(data["messages"]) == 1
        assert "content_blocks" in data["messages"][0]
        # total_count is the parsed-message count; rendered_count paginates groups.
        assert data["total_count"] == 3
        assert data["rendered_count"] == 1
        assert data["returned_count"] == 1
        assert data["degraded"] is False

        mock_reader.get_rendered_window.assert_called_once_with(
            session_id=session.id, limit=100, offset=0, order="head"
        )

    def test_get_messages_returns_codex_tool_search_card(
        self,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test that Codex tool_search pairs reach the messages API as a tool card."""
        session = session_storage.register(
            external_id="codex-tool-search",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
        )
        parser = CodexTranscriptParser()
        records = parser.parse_lines(
            [
                json.dumps(
                    {
                        "timestamp": "2024-06-15T10:30:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "tool_search_call",
                            "call_id": "call-search",
                            "id": "tsc-1",
                            "arguments": {"query": "mcp__gobby list_tools", "limit": 3},
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2024-06-15T10:30:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "tool_search_output",
                            "call_id": "call-search",
                            "tools": [{"name": "mcp__gobby", "type": "namespace", "tool_count": 8}],
                            "tools_count": 1,
                        },
                    }
                ),
            ]
        )
        normalized = normalize_transcript_records(records, source="codex")
        messages: list[ParsedMessage] = []
        for record in normalized:
            assert isinstance(record, ParsedMessage)
            messages.append(record)
        rendered = render_transcript(messages, cli_name="codex", source="codex")

        mock_reader = AsyncMock()
        mock_reader.get_rendered_window = AsyncMock(
            return_value=WindowResult(
                groups=rendered,
                returned_count=1,
                total_groups=1,
                parsed_message_count=len(messages),
            )
        )
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
            transcript_reader=mock_reader,
        )

        response = TestClient(server.app).get(f"/api/sessions/{session.id}/messages")

        assert response.status_code == 200
        data = response.json()
        block = data["messages"][0]["content_blocks"][0]
        assert block["type"] == "tool_chain"
        tool_call = block["tool_calls"][0]
        assert tool_call["id"] == "call-search"
        assert tool_call["tool_name"] == "tool_search"
        assert tool_call["tool_type"] == "search"
        assert tool_call["result"]["content"] == {
            "tools": [{"name": "mcp__gobby", "type": "namespace", "tool_count": 8}],
            "tools_count": 1,
        }

    def test_get_messages_represents_unknown_block_without_parser_error_log(
        self,
        session_storage: SessionManager,
        test_project: dict[str, Any],
        temp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Represented unknown transcript blocks reach the API without parser-error spam."""
        monkeypatch.setenv("HOME", str(temp_dir))
        transcript_path = temp_dir / "codex-unknown.jsonl"
        unknown_payload = {
            "type": "synthetic_unknown_block",
            "id": "synthetic-unknown-1",
            "text": "visible synthetic payload",
            "metadata": {"shape": "future"},
        }
        transcript_path.write_text(
            json.dumps(
                {
                    "timestamp": "2024-06-15T10:30:00Z",
                    "type": "response_item",
                    "payload": unknown_payload,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        session = session_storage.register(
            external_id="codex-unknown-block",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
            transcript_path=str(transcript_path),
        )
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
            transcript_reader=TranscriptReader(session_storage),
        )

        response = TestClient(server.app).get(f"/api/sessions/{session.id}/messages")

        assert response.status_code == 200
        data = response.json()
        block = data["messages"][0]["content_blocks"][0]
        assert block["type"] == "unknown"
        assert block["block_type"] == "response_item/synthetic_unknown_block"
        assert block["content"] == "visible synthetic payload"
        assert block["raw"] == unknown_payload
        log_path = temp_dir / ".gobby" / "logs" / "codex-parser-error.log"
        if log_path.exists():
            assert "Unknown block type" not in log_path.read_text(encoding="utf-8")

    def test_get_messages_tail_order(
        self,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """order=tail is forwarded to the windowed reader."""
        session = session_storage.register(
            external_id="tail-test",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        mock_rendered = MagicMock()
        mock_rendered.to_dict.return_value = {"content_blocks": []}
        mock_reader = AsyncMock()
        mock_reader.get_rendered_window = AsyncMock(
            return_value=WindowResult(
                groups=[mock_rendered],
                returned_count=1,
                total_groups=10,
                parsed_message_count=20,
                degraded=True,
                degraded_reason="max_span_exceeded",
            )
        )

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
            transcript_reader=mock_reader,
        )

        test_client = TestClient(server.app)
        response = test_client.get(
            f"/api/sessions/{session.id}/messages?limit=50&offset=0&order=tail"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["order"] == "tail"
        assert data["rendered_count"] == 10
        assert data["degraded"] is True
        assert data["degraded_reason"] == "max_span_exceeded"
        mock_reader.get_rendered_window.assert_called_once_with(
            session_id=session.id, limit=50, offset=0, order="tail"
        )

    def test_get_messages_rendered_unavailable(
        self,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test 503 if transcript_reader is None but format=rendered requested."""
        session = session_storage.register(
            external_id="unavailable-test",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.transcript_reader = None

        test_client = TestClient(server.app)
        response = test_client.get(f"/api/sessions/{session.id}/messages?format=rendered")

        assert response.status_code == 503
        assert "Transcript reader not available" in response.json()["detail"]

    def test_transcript_status_uses_reader(
        self,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Transcript status should delegate to TranscriptReader when available."""
        session = session_storage.register(
            external_id="status-test",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        mock_reader = AsyncMock()
        mock_reader.get_transcript_status = AsyncMock(
            return_value={
                "session_id": session.id,
                "live_exists": True,
                "archive_exists": False,
                "availability": "live",
                "content_state": "unparseable",
                "session_source": "claude",
                "detected_source": None,
                "source_mismatch": False,
                "raw_record_count": 10,
                "parsed_message_count": 0,
            }
        )

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
            transcript_reader=mock_reader,
        )

        test_client = TestClient(server.app)
        response = test_client.get(f"/api/sessions/{session.id}/transcript/status")

        assert response.status_code == 200
        data = response.json()
        assert data["content_state"] == "unparseable"
        assert data["raw_record_count"] == 10
        mock_reader.get_transcript_status.assert_called_once_with(session.id)
