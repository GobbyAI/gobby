from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.sessions import _blocked_attention_by_session, sessions
from gobby.storage.session_models import Session

pytestmark = pytest.mark.unit

# Mock session data
MOCK_SESSION = Session(
    id="019bbaea-3e0f-7d61-afc4-56a9456c2c7d",
    external_id="ext-123",
    machine_id="21000000-0000-4000-8000-000000000008",
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


def test_blocked_attention_deduplicates_and_sorts_reasons() -> None:
    manager = MagicMock()
    snapshot = SimpleNamespace(
        states=[
            SimpleNamespace(state="blocked", session_id="session-1", reason="reason-b"),
            SimpleNamespace(state="blocked", session_id="session-1", reason="reason-a"),
            SimpleNamespace(state="blocked", session_id="session-1", reason="reason-b"),
            SimpleNamespace(state="active", session_id="session-2", reason="ignored"),
        ]
    )

    with patch("gobby.cli.sessions.AttentionStateManager") as attention_manager:
        attention_manager.return_value.snapshot.return_value = snapshot
        result = _blocked_attention_by_session(manager)

    assert result == {"session-1": (2, "reason-a; reason-b")}
    attention_manager.assert_called_once_with(manager.db)


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


def test_list_sessions_renders_attention_separately_from_lifecycle(
    mock_session_manager,
) -> None:
    """Blocked roster entries render in their own column without changing lifecycle icons."""
    mock_session_manager.list.return_value = [MOCK_SESSION]

    with patch(
        "gobby.cli.sessions._blocked_attention_by_session",
        return_value={MOCK_SESSION.id: (2, "Approval required")},
    ):
        result = CliRunner().invoke(sessions, ["list"])

    assert result.exit_code == 0
    assert result.output.lstrip().startswith("Found 1 sessions:")
    session_row = result.output.splitlines()[-1]
    assert session_row.startswith("●")
    assert "!2 Approval required" in session_row


def test_list_sessions_filters_by_machine_id(mock_session_manager) -> None:
    """Test 'sessions list --machine-id' forwards the filter."""
    mock_session_manager.list.return_value = []

    runner = CliRunner()
    result = runner.invoke(
        sessions, ["list", "--machine-id", "21000000-0000-4000-8000-00000000000a"]
    )

    assert result.exit_code == 0
    assert (
        mock_session_manager.list.call_args.kwargs["machine_id"]
        == "21000000-0000-4000-8000-00000000000a"
    )


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

    assert result.exit_code == 1
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
    # The CLI runtime owns this shared database handle; the command only borrows it.
    mock_session_manager.db.close.assert_not_called()


def test_session_stats(mock_session_manager) -> None:
    """Test 'sessions stats' command."""
    mock_session_manager.list.return_value = [MOCK_SESSION]

    runner = CliRunner()
    result = runner.invoke(sessions, ["stats"])

    assert result.exit_code == 0
    assert "Total Sessions: 1" in result.output
    assert "active: 1" in result.output
    assert "claude_code: 1" in result.output


def test_renumber_sessions_defaults_to_dry_run(mock_session_manager) -> None:
    """Test 'sessions renumber' previews by default."""
    mock_session_manager.renumber_project_sessions.return_value = [
        {
            "session_id": "s1",
            "old_seq_num": 10,
            "new_seq_num": 1,
            "status": "active",
            "title": "First",
        },
        {
            "session_id": "s2",
            "old_seq_num": 30,
            "new_seq_num": 2,
            "status": "active",
            "title": "Second",
        },
    ]

    runner = CliRunner()
    with patch("gobby.cli.sessions._resolve_project_ref_or_path", return_value="proj-1"):
        result = runner.invoke(sessions, ["renumber", "--project", "/tmp/project"])

    assert result.exit_code == 0
    assert "Dry run: scanned 2 session(s) for project proj-1." in result.output
    assert "Mapping count: 2" in result.output
    assert "Changed refs: 2" in result.output
    assert "Old ref range: #10..#30" in result.output
    assert "New ref range: #1..#2" in result.output
    assert "Final max ref: #2" in result.output
    assert "No changes written" in result.output
    mock_session_manager.renumber_project_sessions.assert_called_once_with("proj-1", dry_run=True)


def test_renumber_sessions_apply_writes_refs(mock_session_manager) -> None:
    """Test 'sessions renumber --apply' writes through the storage helper."""
    mock_session_manager.renumber_project_sessions.return_value = [
        {
            "session_id": "s1",
            "old_seq_num": 1,
            "new_seq_num": 1,
            "status": "active",
            "title": "First",
        },
    ]

    runner = CliRunner()
    with patch("gobby.cli.sessions._resolve_project_ref_or_path", return_value="proj-1"):
        result = runner.invoke(sessions, ["renumber", "--project", "proj-1", "--apply"])

    assert result.exit_code == 0
    assert "Applied: scanned 1 session(s) for project proj-1." in result.output
    assert "Changed refs: 0" in result.output
    assert "Final max ref: #1" in result.output
    assert "No changes written" not in result.output
    mock_session_manager.renumber_project_sessions.assert_called_once_with("proj-1", dry_run=False)


def test_renumber_sessions_rejects_removed_dry_run_option(mock_session_manager) -> None:
    """Test 'sessions renumber' has --apply as the only explicit mode flag."""
    runner = CliRunner()
    result = runner.invoke(
        sessions,
        ["renumber", "--project", "proj-1", "--dry-run", "--apply"],
    )

    assert result.exit_code != 0
    assert "No such option '--dry-run'" in result.output
    mock_session_manager.renumber_project_sessions.assert_not_called()
