from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.sessions import sessions
from gobby.sessions.wiki_synthesis import WikiBackfillFailure, WikiBackfillResult
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
    mock_session_manager.db.close.assert_called_once_with()


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
    assert "No such option: --dry-run" in result.output
    mock_session_manager.renumber_project_sessions.assert_not_called()


class TestBackfillWiki:
    """Tests for the `sessions backfill-wiki` command."""

    @staticmethod
    def _config(*, wiki_enabled: bool = True):
        config = MagicMock()
        config.session_wiki = SimpleNamespace(
            enabled=wiki_enabled,
            prompt_path="wiki/source_page",
            wiki_file_path=".gobby/session_wiki",
        )
        config.session_summary = MagicMock()
        return config

    def test_dry_run_reports_counts_without_building_llm(self) -> None:
        config = self._config()
        backfill_mock = AsyncMock(return_value=WikiBackfillResult(scanned=5, eligible=3, skipped=2))
        with (
            patch("gobby.cli.sessions.get_session_manager"),
            patch("gobby.config.app.load_config", return_value=config),
            patch("gobby.llm.factory.create_llm_service") as create_llm,
            patch("gobby.sessions.wiki_synthesis.backfill_session_wikis", backfill_mock),
        ):
            result = CliRunner().invoke(sessions, ["backfill-wiki", "--dry-run"])

        assert result.exit_code == 0
        assert "Would synthesize 3" in result.output
        create_llm.assert_not_called()  # dry-run never needs an LLM service
        _, kwargs = backfill_mock.call_args
        assert kwargs["dry_run"] is True
        assert kwargs["llm_service"] is None

    def test_disabled_config_short_circuits(self) -> None:
        config = self._config(wiki_enabled=False)
        backfill_mock = AsyncMock()
        with (
            patch("gobby.cli.sessions.get_session_manager"),
            patch("gobby.config.app.load_config", return_value=config),
            patch("gobby.sessions.wiki_synthesis.backfill_session_wikis", backfill_mock),
        ):
            result = CliRunner().invoke(sessions, ["backfill-wiki", "--dry-run"])

        assert result.exit_code == 0
        assert "disabled" in result.output
        backfill_mock.assert_not_called()

    def test_real_run_reports_failures(self) -> None:
        config = self._config()
        backfill_mock = AsyncMock(
            return_value=WikiBackfillResult(
                scanned=2,
                eligible=2,
                synthesized=1,
                failed=1,
                failures=[WikiBackfillFailure("abc123def456", "llm_error", "boom")],
            )
        )
        with (
            patch("gobby.cli.sessions.get_session_manager"),
            patch("gobby.config.app.load_config", return_value=config),
            patch("gobby.llm.factory.create_llm_service"),
            patch("gobby.sessions.wiki_synthesis.backfill_session_wikis", backfill_mock),
        ):
            result = CliRunner().invoke(sessions, ["backfill-wiki"])

        assert result.exit_code == 0
        assert "Synthesized 1 of 2" in result.output
        assert "abc123def456" in result.output
        assert "boom" in result.output
