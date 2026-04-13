from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.sessions import _format_turns_for_llm, sessions
from gobby.storage.session_models import Session

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_session_manager():
    with patch("gobby.cli.sessions.get_session_manager") as mock:
        yield mock.return_value


@pytest.fixture
def mock_resolve_session():
    with patch("gobby.cli.sessions.resolve_session_id") as mock:
        mock.side_effect = lambda x: x if x else "current-session-id"
        yield mock


@pytest.fixture
def mock_resolve_project():
    with patch("gobby.cli.sessions.resolve_project_ref") as mock:
        mock.side_effect = lambda x: x
        yield mock


async def async_return(val):
    return val


def test_list_sessions_empty(mock_session_manager) -> None:
    mock_session_manager.list.return_value = []

    runner = CliRunner()
    result = runner.invoke(sessions, ["list"])

    assert result.exit_code == 0
    assert "No sessions found" in result.output


def test_list_sessions_found(mock_session_manager) -> None:
    session = Session(
        id="sess-1",
        project_id="proj-1",
        status="active",
        created_at="2023-01-01T00:00:00",
        updated_at="2023-01-01T00:00:00",
        source="claude",
        title="Test Session",
        seq_num=1,
        external_id=None,
        machine_id=None,
        transcript_path=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
    )
    mock_session_manager.list.return_value = [session]

    runner = CliRunner()
    result = runner.invoke(sessions, ["list"])

    assert result.exit_code == 0
    assert "Found 1 sessions" in result.output
    assert "sess-1" in result.output
    assert "Test Session" in result.output
    assert "#1" in result.output


def test_list_sessions_json(mock_session_manager) -> None:
    session = Session(
        id="sess-1",
        project_id="proj-1",
        status="active",
        created_at="2023-01-01T00:00:00",
        updated_at="2023-01-01T00:00:00",
        source="claude",
        title="Test Session",
        external_id=None,
        machine_id=None,
        transcript_path=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
    )
    mock_session_manager.list.return_value = [session]

    runner = CliRunner()
    result = runner.invoke(sessions, ["list", "--json"])

    assert result.exit_code == 0
    assert '"id": "sess-1"' in result.output
    assert '"title": "Test Session"' in result.output


def test_show_session_found(mock_session_manager, mock_resolve_session) -> None:
    session = Session(
        id="sess-1",
        project_id="proj-1",
        status="active",
        created_at="2023-01-01T00:00:00",
        updated_at="2023-01-01T00:00:00",
        source="claude",
        title="Test Session",
        summary_markdown="Test Summary",
        external_id=None,
        machine_id=None,
        transcript_path=None,
        summary_path=None,
        git_branch=None,
        parent_session_id=None,
    )
    mock_session_manager.get.return_value = session

    runner = CliRunner()
    result = runner.invoke(sessions, ["show", "sess-1"])

    assert result.exit_code == 0
    assert "Session: sess-1" in result.output
    assert "Summary:" in result.output
    assert "Test Summary" in result.output


def test_show_session_not_found(mock_session_manager, mock_resolve_session) -> None:
    mock_session_manager.get.return_value = None

    runner = CliRunner()
    result = runner.invoke(sessions, ["show", "missing"])

    assert result.exit_code == 0
    assert "Session not found" in result.output


def test_delete_session_success(mock_session_manager, mock_resolve_session) -> None:
    session = Session(
        id="sess-1",
        project_id="proj-1",
        status="active",
        created_at="2023-01-01T00:00:00",
        updated_at="2023-01-01T00:00:00",
        source="claude",
        title=None,
        external_id=None,
        machine_id=None,
        transcript_path=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
    )
    mock_session_manager.get.return_value = session
    mock_session_manager.delete.return_value = True

    runner = CliRunner()
    result = runner.invoke(sessions, ["delete", "sess-1", "--yes"])

    assert result.exit_code == 0
    assert "Deleted session: sess-1" in result.output
    mock_session_manager.delete.assert_called_with("sess-1")


def test_delete_session_not_found(mock_session_manager, mock_resolve_session) -> None:
    mock_session_manager.get.return_value = None

    runner = CliRunner()
    result = runner.invoke(sessions, ["delete", "missing", "--yes"])

    assert result.exit_code == 0
    assert "Session not found" in result.output


def test_session_stats(mock_session_manager) -> None:
    s1 = Session(
        id="s1",
        project_id="p1",
        status="active",
        source="claude",
        created_at="",
        updated_at="",
        title=None,
        external_id=None,
        machine_id=None,
        transcript_path=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
    )
    s2 = Session(
        id="s2",
        project_id="p1",
        status="completed",
        source="gemini",
        created_at="",
        updated_at="",
        title=None,
        external_id=None,
        machine_id=None,
        transcript_path=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
    )
    mock_session_manager.list.return_value = [s1, s2]

    runner = CliRunner()
    result = runner.invoke(sessions, ["stats"])

    assert result.exit_code == 0
    assert "Total Sessions: 2" in result.output
    assert "active: 1" in result.output
    assert "completed: 1" in result.output
    assert "claude: 1" in result.output
    assert "gemini: 1" in result.output


def test_show_messages(mock_session_manager, mock_resolve_session) -> None:
    session = Session(
        id="s1",
        project_id="p1",
        status="active",
        source="claude",
        created_at="",
        updated_at="",
        title=None,
        external_id=None,
        machine_id=None,
        transcript_path=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
    )
    mock_session_manager.get.return_value = session

    msgs = [
        {"role": "user", "content": "hello", "message_index": 1},
        {"role": "assistant", "content": "hi", "message_index": 2},
    ]

    with patch("gobby.sessions.transcript_reader.TranscriptReader") as mock_reader_cls:
        mock_reader = MagicMock()
        mock_reader_cls.return_value = mock_reader
        mock_reader.get_messages = lambda **kwargs: async_return(msgs)
        mock_reader.count_messages = lambda session_id: async_return(2)

        runner = CliRunner()
        result = runner.invoke(sessions, ["messages", "s1"])

    assert result.exit_code == 0
    assert "Messages for session s1" in result.output
    assert "user: hello" in result.output
    assert "assistant: hi" in result.output


@pytest.mark.integration
@patch("gobby.storage.projects.LocalProjectManager")
@patch("gobby.cli.sessions.LocalDatabase")
@patch("subprocess.run")
@patch("gobby.sessions.analyzer.TranscriptAnalyzer")
@patch("pathlib.Path.exists")
@patch("builtins.open")
def test_create_handoff(
    mock_open,
    mock_exists,
    mock_analyzer,
    mock_subprocess,
    mock_db,
    mock_project_manager,
    mock_session_manager,
    mock_resolve_session,
):
    session = Session(
        id="s1",
        project_id="p1",
        status="active",
        source="claude",
        created_at="",
        updated_at="",
        transcript_path="/tmp/transcript.jsonl",
        title=None,
        external_id=None,
        machine_id=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
    )
    mock_session_manager.get.return_value = session
    mock_exists.return_value = True

    # Mock transcript reading needs to return a context manager that yields lines
    mock_file = MagicMock()
    mock_file.__enter__.return_value = ['{"role": "user", "content": "hello"}']
    mock_open.return_value = mock_file

    # Mock Analyzer
    mock_ctx = MagicMock()
    mock_ctx.active_gobby_task = None
    mock_ctx.todo_state = []
    mock_ctx.files_modified = []
    mock_ctx.git_commits = []
    mock_ctx.initial_goal = None
    mock_ctx.git_status = ""
    mock_analyzer.return_value.extract_handoff_context.return_value = mock_ctx

    # Mock generate_session_summaries to avoid LLM calls
    async def mock_gen_summaries(**kwargs):
        return {"success": True, "full_length": 100}

    # After generate_session_summaries succeeds, the command re-fetches the session
    updated_session = Session(
        id="s1",
        project_id="p1",
        status="active",
        source="claude",
        created_at="",
        updated_at="",
        transcript_path="/tmp/transcript.jsonl",
        title=None,
        external_id=None,
        machine_id=None,
        summary_path=None,
        summary_markdown="# Handoff Summary\nTest content",
        git_branch=None,
        parent_session_id=None,
    )
    mock_session_manager.get.side_effect = [session, updated_session]

    runner = CliRunner()
    with patch(
        "gobby.cli.sessions.asyncio.run", return_value={"success": True, "full_length": 100}
    ):
        result = runner.invoke(sessions, ["create-handoff", "-s", "s1", "--output", "db"])

    assert result.exit_code == 0
    assert "Created handoff context" in result.output


@pytest.mark.integration
@patch("gobby.storage.projects.LocalProjectManager")
@patch("gobby.cli.sessions.LocalDatabase")
@patch("subprocess.run")
@patch("gobby.sessions.analyzer.TranscriptAnalyzer")
@patch("pathlib.Path.exists")
@patch("builtins.open")
def test_create_handoff_full_llm_error(
    mock_open,
    mock_exists,
    mock_analyzer,
    mock_subprocess,
    mock_db,
    mock_project_manager,
    mock_session_manager,
    mock_resolve_session,
):
    session = Session(
        id="s1",
        project_id="p1",
        status="active",
        source="claude",
        created_at="",
        updated_at="",
        transcript_path="/tmp/transcript.jsonl",
        title=None,
        external_id=None,
        machine_id=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
        usage_input_tokens=0,
        usage_output_tokens=0,
        usage_cache_creation_tokens=0,
        usage_cache_read_tokens=0,
        agent_depth=0,
        spawned_by_agent_id=None,
        workflow_name=None,
        agent_run_id=None,
        context_injected=False,
        original_prompt=None,
    )
    mock_session_manager.get.return_value = session
    mock_exists.return_value = True

    # Mock transcript
    mock_file = MagicMock()
    mock_file.__enter__.return_value = ['{"role": "user", "content": "hello"}']
    mock_open.return_value = mock_file

    # Mock Analyzer
    mock_ctx = MagicMock()
    mock_ctx.active_gobby_task = None
    mock_ctx.todo_state = []
    mock_ctx.files_modified = []
    mock_ctx.git_commits = []
    mock_ctx.initial_goal = None
    mock_ctx.git_status = ""
    mock_analyzer.return_value.extract_handoff_context.return_value = mock_ctx

    runner = CliRunner()
    # Mock asyncio.run to raise, triggering the fallback path
    with patch("gobby.cli.sessions.asyncio.run", side_effect=Exception("Config error")):
        result = runner.invoke(sessions, ["create-handoff", "-s", "s1", "--output", "db"])

    # Should gracefully fall back to code-only summary
    assert result.exit_code == 0
    assert "Warning: LLM summary failed" in result.output
    assert "Created handoff context" in result.output


def test_create_handoff_no_session(mock_session_manager, mock_resolve_session) -> None:
    mock_session_manager.get.return_value = None
    runner = CliRunner()
    result = runner.invoke(sessions, ["create-handoff", "-s", "missing"])
    assert result.exit_code == 0
    assert "Session not found" in result.output


def test_create_handoff_no_transcript_path(mock_session_manager, mock_resolve_session) -> None:
    session = Session(
        id="s1",
        project_id="p1",
        status="active",
        source="claude",
        created_at="",
        updated_at="",
        transcript_path=None,  # No path
        title=None,
        external_id=None,
        machine_id=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
        usage_input_tokens=0,
        usage_output_tokens=0,
        usage_cache_creation_tokens=0,
        usage_cache_read_tokens=0,
        agent_depth=0,
        spawned_by_agent_id=None,
        workflow_name=None,
        agent_run_id=None,
        context_injected=False,
        original_prompt=None,
    )
    mock_session_manager.get.return_value = session
    runner = CliRunner()
    result = runner.invoke(sessions, ["create-handoff", "-s", "s1"])
    assert result.exit_code == 0
    assert "has no transcript path" in result.output


def test_create_handoff_transcript_not_found(mock_session_manager, mock_resolve_session) -> None:
    session = Session(
        id="s1",
        project_id="p1",
        status="active",
        source="claude",
        created_at="",
        updated_at="",
        transcript_path="/tmp/missing.jsonl",
        title=None,
        external_id=None,
        machine_id=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
        usage_input_tokens=0,
        usage_output_tokens=0,
        usage_cache_creation_tokens=0,
        usage_cache_read_tokens=0,
        agent_depth=0,
        spawned_by_agent_id=None,
        workflow_name=None,
        agent_run_id=None,
        context_injected=False,
        original_prompt=None,
    )
    mock_session_manager.get.return_value = session

    with patch("pathlib.Path.exists", return_value=False):
        runner = CliRunner()
        result = runner.invoke(sessions, ["create-handoff", "-s", "s1"])

    assert result.exit_code == 0
    assert "Transcript file not found" in result.output


def test_list_sessions_filters(mock_session_manager) -> None:
    runner = CliRunner()

    # Test strict filters call manager with correct args
    result = runner.invoke(
        sessions, ["list", "--status", "active", "--source", "claude", "--limit", "5"]
    )
    assert result.exit_code == 0

    mock_session_manager.list.assert_called_with(
        project_id=None, status="active", source="claude", limit=5
    )


def test_list_sessions_project_filter(mock_session_manager) -> None:
    with patch("gobby.cli.sessions.resolve_project_ref", return_value="p1"):
        runner = CliRunner()
        result = runner.invoke(sessions, ["list", "--project", "my-project"])

        assert result.exit_code == 0
        mock_session_manager.list.assert_called_with(
            project_id="p1", status=None, source=None, limit=20
        )


def test_format_turns_for_llm() -> None:
    turns = [
        {"message": {"role": "user", "content": "hello"}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "name": "test_tool"},
                ],
            }
        },
    ]
    result = _format_turns_for_llm(turns)
    assert "[Turn 1 - user]: hello" in result
    assert "[Turn 2 - assistant]: hi [Tool: test_tool]" in result


@pytest.mark.integration
def test_create_handoff_full_success(mock_session_manager, mock_resolve_session):
    session = Session(
        id="s1",
        project_id="p1",
        status="active",
        source="claude",
        created_at="",
        updated_at="",
        transcript_path="/tmp/transcript.jsonl",
        title=None,
        external_id=None,
        machine_id=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
    )

    # After generate_session_summaries succeeds, the command re-fetches the session
    updated_session = Session(
        id="s1",
        project_id="p1",
        status="active",
        source="claude",
        created_at="",
        updated_at="",
        transcript_path="/tmp/transcript.jsonl",
        title=None,
        external_id=None,
        machine_id=None,
        summary_path=None,
        summary_markdown="Full Summary Content",
        git_branch=None,
        parent_session_id=None,
    )
    mock_session_manager.get.side_effect = [session, updated_session]

    # Setup Mocks
    with (
        patch("builtins.open") as mock_open,
        patch("pathlib.Path.exists", return_value=True),
        patch("gobby.sessions.analyzer.TranscriptAnalyzer") as mock_analyzer,
        patch("subprocess.run"),
        patch("gobby.cli.sessions.LocalDatabase"),
        patch("gobby.storage.projects.LocalProjectManager"),
    ):
        # Mock file reading
        mock_file = MagicMock()
        mock_file.__enter__.return_value = ['{"role": "user", "content": "hello"}']
        mock_open.return_value = mock_file

        # Mock Analyzer
        mock_ctx = MagicMock()
        mock_ctx.git_status = "clean"
        mock_analyzer.return_value.extract_handoff_context.return_value = mock_ctx

        # Mock asyncio.run to simulate successful summary generation
        with patch(
            "gobby.cli.sessions.asyncio.run",
            return_value={"success": True, "full_length": 100},
        ):
            runner = CliRunner()
            result = runner.invoke(sessions, ["create-handoff", "-s", "s1", "--output", "db"])

        assert result.exit_code == 0
        assert "Created handoff context" in result.output

        # Verify update_summary was called with the full markdown from the re-fetched session
        mock_session_manager.update_summary.assert_called_once()
        args, kwargs = mock_session_manager.update_summary.call_args
        assert args[0] == "s1"
        assert kwargs.get("summary_markdown") == "Full Summary Content"
