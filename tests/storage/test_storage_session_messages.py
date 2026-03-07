from datetime import datetime
from unittest.mock import MagicMock

import pytest

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.database import LocalDatabase
from gobby.storage.session_messages import LocalSessionMessageManager

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_db():
    return MagicMock(spec=LocalDatabase)


@pytest.fixture
def manager(mock_db):
    return LocalSessionMessageManager(mock_db)


@pytest.fixture
def sample_message():
    return ParsedMessage(
        index=1,
        role="user",
        content="Hello",
        content_type="text",
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime.fromisoformat("2024-01-01T12:00:00"),
        raw_json={"type": "user", "message": "Hello"},
    )


@pytest.mark.asyncio
async def test_store_messages_empty(manager):
    assert await manager.store_messages("session-1", []) == 0
    manager.db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_store_messages_single(manager, mock_db, sample_message):
    count = await manager.store_messages("session-1", [sample_message])

    assert count == 1
    mock_db.execute.assert_called_once()

    # Verify arguments passed to execute
    call_args = mock_db.execute.call_args
    assert call_args is not None
    query, params = call_args[0]

    assert "INSERT INTO session_messages" in query
    assert params[0] == "session-1"
    assert params[1] == sample_message.index
    assert params[2] == sample_message.role
    assert params[3] == sample_message.content
    assert params[8] == sample_message.tool_use_id  # tool_use_id
    assert params[9] == sample_message.timestamp.isoformat()


@pytest.mark.asyncio
async def test_store_messages_with_tools(manager, mock_db):
    msg = ParsedMessage(
        index=2,
        role="assistant",
        content="",
        content_type="tool_use",
        tool_name="test_tool",
        tool_input={"arg": "val"},
        tool_result=None,
        timestamp=datetime.now(),
        raw_json={},
    )

    await manager.store_messages("session-1", [msg])

    call_args = mock_db.execute.call_args
    _, params = call_args[0]

    # Verify tool input was JSON encoded
    assert params[5] == "test_tool"
    assert params[6] == '{"arg": "val"}'


@pytest.mark.asyncio
async def test_get_messages(manager, mock_db):
    mock_db.fetchall.return_value = [
        {"session_id": "s1", "message_index": 1, "content": "msg1"},
        {"session_id": "s1", "message_index": 2, "content": "msg2"},
    ]

    messages = await manager.get_messages("s1")

    assert len(messages) == 2
    assert messages[0]["content"] == "msg1"

    # Check query structure
    call_args = mock_db.fetchall.call_args
    query, params = call_args[0]
    assert "WHERE session_id = ?" in query
    assert params[0] == "s1"


@pytest.mark.asyncio
async def test_get_messages_with_filters(manager, mock_db):
    mock_db.fetchall.return_value = []

    await manager.get_messages("s1", role="user", limit=10, offset=5)

    call_args = mock_db.fetchall.call_args
    query, params = call_args[0]

    assert "AND role = ?" in query
    assert "user" in params
    assert params[-2] == 10  # limit
    assert params[-1] == 5  # offset


@pytest.mark.asyncio
async def test_get_state(manager, mock_db):
    mock_db.fetchone.return_value = {
        "session_id": "s1",
        "last_byte_offset": 100,
        "last_message_index": 5,
    }

    state = await manager.get_state("s1")

    assert state is not None
    assert state["last_byte_offset"] == 100
    mock_db.fetchone.assert_called_with(
        "SELECT * FROM session_message_state WHERE session_id = ?", ("s1",)
    )


@pytest.mark.asyncio
async def test_get_state_none(manager, mock_db):
    mock_db.fetchone.return_value = None
    state = await manager.get_state("s1")
    assert state is None


@pytest.mark.asyncio
async def test_update_state(manager, mock_db):
    await manager.update_state("s1", byte_offset=200, message_index=10)

    call_args = mock_db.execute.call_args
    query, params = call_args[0]

    assert "INSERT INTO session_message_state" in query
    assert params[0] == "s1"
    assert params[1] == 200  # byte_offset
    assert params[2] == 10  # message_index


@pytest.mark.asyncio
async def test_store_messages_error(manager, mock_db, sample_message):
    """Test that store_messages propagates exceptions."""
    mock_db.execute.side_effect = Exception("DB Error")

    with pytest.raises(Exception, match="DB Error"):
        await manager.store_messages("session-1", [sample_message])


@pytest.mark.asyncio
async def test_count_messages(manager, mock_db):
    """Test counting messages for a session."""
    mock_db.fetchone.return_value = {"count": 42}

    count = await manager.count_messages("session-1")

    assert count == 42
    mock_db.fetchone.assert_called_with(
        "SELECT COUNT(*) as count FROM session_messages WHERE session_id = ?",
        ("session-1",),
    )


@pytest.mark.asyncio
async def test_count_messages_none(manager, mock_db):
    """Test counting messages when result is None (should not happen but handle it)."""
    mock_db.fetchone.return_value = None
    count = await manager.count_messages("session-1")
    assert count == 0


@pytest.mark.asyncio
async def test_get_all_counts(manager, mock_db):
    """Test getting counts for all sessions."""
    mock_db.fetchall.return_value = [
        {"session_id": "s1", "count": 10},
        {"session_id": "s2", "count": 5},
    ]

    counts = await manager.get_all_counts()

    assert counts == {"s1": 10, "s2": 5}
    mock_db.fetchall.assert_called_once()


@pytest.mark.asyncio
async def test_search_messages(manager, mock_db):
    """Test searching messages."""
    mock_db.fetchall.return_value = [
        {"content": "result 1", "session_id": "s1"},
    ]

    results = await manager.search_messages("query term", limit=5)

    assert len(results) == 1
    call_args = mock_db.fetchall.call_args
    query, params = call_args[0]

    assert "LIKE ? ESCAPE '\\'" in query
    assert "%query term%" in params
    assert params[-2] == 5  # limit


@pytest.mark.asyncio
async def test_search_messages_with_filters(manager, mock_db):
    """Test searching messages with project and session filters."""
    mock_db.fetchall.return_value = []

    await manager.search_messages("query", session_id="s1", project_id="p1")

    call_args = mock_db.fetchall.call_args
    query, params = call_args[0]

    assert "JOIN sessions s ON m.session_id = s.session_id" in query
    assert "s.project_id = ?" in query
    assert "m.session_id = ?" in query
    assert "p1" in params
    assert "s1" in params
