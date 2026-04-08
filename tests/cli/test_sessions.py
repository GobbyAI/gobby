from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gobby.cli.sessions import sessions
from gobby.storage.session_models import Session

pytestmark = pytest.mark.unit

# Mock session data
MOCK_SESSION = Session(
    id="019bbaea-3e0f-7d61-afc4-56a9456c2c7d",
    external_id="ext-123",
    machine_id="machine-123",
    source="claude_code",
    project_id="test-project",
    title="Test Session",
    status="active",
    transcript_path="/tmp/test.jsonl",
    summary_path=None,
    summary_markdown=None,
    git_branch="main",
    parent_session_id=None,
    created_at=datetime.now(UTC).isoformat(),
    updated_at=datetime.now(UTC).isoformat(),
    seq_num=42,
)


@pytest.fixture
def mock_session_manager():
    with patch("gobby.cli.sessions.get_session_manager") as mock:
        yield mock.return_value


def test_list_sessions_empty(mock_session_manager) -> None:
    """Test 'sessions list' with no sessions."""
    mock_session_manager.list.return_value = []

    runner = CliRunner()
    result = runner.invoke(sessions, ["list"])

    assert result.exit_code == 0
    assert "No sessions found" in result.output
    mock_session_manager.list.assert_called_once()


def test_list_sessions_populated(mock_session_manager) -> None:
    """Test 'sessions list' with active sessions."""
    mock_session_manager.list.return_value = [MOCK_SESSION]

    runner = CliRunner()
    result = runner.invoke(sessions, ["list"])

    assert result.exit_code == 0
    # Check for icon, sequence number, and title
    assert "●" in result.output
    assert "#42" in result.output
    assert "Test Session" in result.output


def test_show_session_found(mock_session_manager) -> None:
    """Test 'sessions show' with valid ID."""
    mock_session_manager.get.return_value = MOCK_SESSION

    runner = CliRunner()
    with patch("gobby.cli.sessions.resolve_session_id", return_value=MOCK_SESSION.id):
        result = runner.invoke(sessions, ["show", MOCK_SESSION.id])

    assert result.exit_code == 0
    assert f"Session: {MOCK_SESSION.id}" in result.output
    assert "Status: active" in result.output
    assert "Title: Test Session" in result.output


def test_show_session_not_found(mock_session_manager) -> None:
    """Test 'sessions show' with invalid ID."""
    mock_session_manager.get.return_value = None

    runner = CliRunner()
    # Mock resolve_session_id to return input
    with patch("gobby.cli.sessions.resolve_session_id", side_effect=lambda x: x):
        result = runner.invoke(sessions, ["show", "invalid-id"])

    assert result.exit_code == 0
    assert "Session not found: invalid-id" in result.output


def test_delete_session_success(mock_session_manager) -> None:
    """Test 'sessions delete' with confirmation."""
    mock_session_manager.get.return_value = MOCK_SESSION
    mock_session_manager.delete.return_value = True

    runner = CliRunner()
    with patch("gobby.cli.sessions.resolve_session_id", return_value=MOCK_SESSION.id):
        # Pass input="y" for confirmation
        result = runner.invoke(sessions, ["delete", MOCK_SESSION.id], input="y\n")

    assert result.exit_code == 0
    assert f"Deleted session: {MOCK_SESSION.id}" in result.output
    mock_session_manager.delete.assert_called_once_with(MOCK_SESSION.id)


def test_session_stats(mock_session_manager) -> None:
    """Test 'sessions stats' command."""
    mock_session_manager.list.return_value = [MOCK_SESSION]

    runner = CliRunner()
    result = runner.invoke(sessions, ["stats"])

    assert result.exit_code == 0
    assert "Total Sessions: 1" in result.output
    assert "active: 1" in result.output
    assert "claude_code: 1" in result.output
