from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.database import LocalDatabase
from gobby.storage.session_messages import LocalSessionMessageManager
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_db():
    db = LocalDatabase(":memory:")

    # Create shared connection
    import sqlite3

    shared_conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    shared_conn.row_factory = sqlite3.Row
    shared_conn.execute("PRAGMA foreign_keys = ON")
    # WAL mode is not supported for in-memory databases, skip it

    # Patch _get_connection to return the shared connection
    db._get_connection = lambda: shared_conn  # type: ignore

    # Create tables using the shared connection
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            external_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            source TEXT NOT NULL,
            project_id TEXT,
            jsonl_path TEXT,
            title TEXT,
            status TEXT DEFAULT 'active',
            git_branch TEXT,
            parent_session_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            summary_path TEXT,
            summary_markdown TEXT,
            compact_markdown TEXT,
            agent_depth INTEGER DEFAULT 0,
            spawned_by_agent_id TEXT,
            workflow_name TEXT,
            agent_run_id TEXT,
            context_injected INTEGER DEFAULT 0,
            original_prompt TEXT,
            transcript_processed BOOLEAN DEFAULT FALSE,
            terminal_context TEXT,
            usage_input_tokens INTEGER DEFAULT 0,
            usage_output_tokens INTEGER DEFAULT 0,
            usage_cache_creation_tokens INTEGER DEFAULT 0,
            usage_cache_read_tokens INTEGER DEFAULT 0,
            usage_total_cost_usd REAL DEFAULT 0.0,
            seq_num INTEGER,
            had_edits BOOLEAN DEFAULT 0,
            UNIQUE(external_id, machine_id, source)
        );
    """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            message_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            content_type TEXT DEFAULT 'text',
            tool_name TEXT,
            tool_input TEXT,
            tool_result TEXT,
            timestamp TEXT NOT NULL,
            raw_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(session_id, message_index)
        );
    """
    )
    # Add session_message_state for completeness (though not used directly here)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_message_state (
            session_id TEXT PRIMARY KEY,
            last_byte_offset INTEGER DEFAULT 0,
            last_message_index INTEGER DEFAULT 0,
            last_processed_at TEXT,
            processing_errors INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """
    )
    # Auth middleware checks this table
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS secrets (
            name TEXT PRIMARY KEY,
            encrypted_value TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """
    )
    return db


@pytest.fixture
def server(mock_db):
    session_manager = LocalSessionManager(mock_db)
    message_manager = LocalSessionMessageManager(mock_db)
    task_manager = LocalTaskManager(mock_db)

    # Create minimal config mock
    mock_config = MagicMock()
    mock_config.websocket = None

    services = ServiceContainer(
        config=mock_config,
        database=mock_db,
        session_manager=session_manager,
        task_manager=task_manager,
        message_manager=message_manager,
    )
    server = HTTPServer(services=services, port=0, test_mode=True)
    return server


@pytest.fixture
def client(server):
    return TestClient(server.app)


@pytest.mark.asyncio
async def test_get_session_messages(client, mock_db):
    # Setup data
    session_manager = LocalSessionManager(mock_db)
    message_manager = LocalSessionMessageManager(mock_db)

    session = session_manager.register(
        external_id="ext-1",
        machine_id="mach-1",
        source="test",
        project_id="proj-1",
        jsonl_path="/tmp/test.jsonl",
    )

    msg1 = ParsedMessage(
        index=0,
        role="user",
        content="Hello",
        content_type="text",
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime.now(),
        raw_json={},
    )
    msg2 = ParsedMessage(
        index=1,
        role="assistant",
        content="Hi there",
        content_type="text",
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime.now(),
        raw_json={},
    )

    await message_manager.store_messages(session.id, [msg1, msg2])

    # Test endpoint
    response = client.get(f"/api/sessions/{session.id}/messages")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["total_count"] == 2
    assert len(data["messages"]) == 2
    assert data["messages"][0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_list_sessions_with_counts(client, mock_db):
    # Setup data
    session_manager = LocalSessionManager(mock_db)
    message_manager = LocalSessionMessageManager(mock_db)

    # Create two sessions
    session1 = session_manager.register(
        external_id="ext-1", machine_id="mach-1", source="test", project_id="proj-1"
    )
    session2 = session_manager.register(
        external_id="ext-2", machine_id="mach-2", source="test", project_id="proj-1"
    )

    # Add messages to session 1
    msg1 = ParsedMessage(
        index=0,
        role="user",
        content="Hello",
        content_type="text",
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime.now(),
        raw_json={},
    )
    await message_manager.store_messages(session1.id, [msg1])

    # Test list endpoint
    response = client.get("/api/sessions")
    assert response.status_code == 200
    data = response.json()

    sessions = data["sessions"]
    assert len(sessions) >= 2

    s1 = next(s for s in sessions if s["id"] == session1.id)
    s2 = next(s for s in sessions if s["id"] == session2.id)

    assert s1["message_count"] == 1
    assert s2["message_count"] == 0


@pytest.mark.asyncio
async def test_get_session_messages_not_found_manager(client, server):
    # Test error handling when manager missing
    server.message_manager = None
    response = client.get("/api/sessions/some-id/messages")
    assert response.status_code == 503
