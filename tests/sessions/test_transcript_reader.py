"""Tests for TranscriptReader — JSONL + gzip archive read layer."""

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.transcript_paths import _find_transcript_on_disk
from gobby.sessions.transcript_reader import TranscriptReader, _filter_messages, clear_archive_cache
from gobby.sessions.transcript_renderer import RenderedMessage


# Helper to write a plain JSONL file (not gzipped)
def _write_jsonl_file(path: Path, lines: list[dict]) -> Path:
    """Write JSONL lines to a plain file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def _make_codex_message(role: str, text: str, ts: str) -> dict:
    block_type = "input_text" if role == "user" else "output_text"
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": block_type, "text": text}],
        },
    }


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear LRU cache before each test."""
    clear_archive_cache()
    yield
    clear_archive_cache()


def _make_msg_dict(index: int, role: str = "assistant", content: str = "hi") -> dict:
    return {
        "session_id": "sess-1",
        "message_index": index,
        "role": role,
        "content": content,
        "content_type": "text",
        "tool_name": None,
        "tool_input": None,
        "tool_result": None,
        "tool_use_id": None,
        "timestamp": datetime.now(UTC).isoformat(),
        "raw_json": {},
    }


def test_filter_messages_does_not_mutate_input() -> None:
    messages = [{"role": "user", "content": "hello"}]

    result = _filter_messages(messages, session_id="sess-1", role=None)

    assert result == [{"role": "user", "content": "hello", "session_id": "sess-1"}]
    assert messages == [{"role": "user", "content": "hello"}]


def test_codex_transcript_scan_respects_exact_max_days(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sessions = tmp_path / ".codex" / "sessions"
    for day in ("03", "02", "01"):
        (sessions / "2026" / "05" / day).mkdir(parents=True)
    target = sessions / "2026" / "05" / "01" / "rollout-ext-abc.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    assert _find_transcript_on_disk("codex", "ext-abc", max_days=2) is None
    assert _find_transcript_on_disk("codex", "ext-abc", max_days=3) == str(target)


def _write_gzip_archive(archive_dir: Path, external_id: str, lines: list[dict]) -> Path:
    """Write JSONL lines to a gzip archive."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{external_id}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


class TestTranscriptReaderGzipFallback:
    """TranscriptReader falls back to gzip archive when JSONL is absent."""

    @pytest.mark.asyncio
    async def test_falls_back_to_gzip(self, tmp_path: Path):
        archive_dir = tmp_path / "archives"
        external_id = "ext-abc123"

        lines = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        ]
        _write_gzip_archive(archive_dir, external_id, lines)

        session = MagicMock()
        session.external_id = external_id
        session.source = "claude"
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))
        result = await reader.get_messages("sess-1", limit=50)

        assert len(result) > 0
        for msg in result:
            assert msg["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_count_falls_back_to_gzip(self, tmp_path: Path):
        archive_dir = tmp_path / "archives"
        external_id = "ext-count"

        lines = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        ]
        _write_gzip_archive(archive_dir, external_id, lines)

        session = MagicMock()
        session.external_id = external_id
        session.source = "claude"
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))
        count = await reader.count_messages("sess-1")

        assert count > 0

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_archive(self, tmp_path: Path):
        archive_dir = tmp_path / "empty-archives"
        archive_dir.mkdir()

        session = MagicMock()
        session.external_id = "no-archive"
        session.source = "claude"
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))
        result = await reader.get_messages("sess-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_external_id(self):
        session = MagicMock()
        session.external_id = None
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)
        result = await reader.get_messages("sess-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_session_not_found(self):
        session_manager = MagicMock()
        session_manager.get.return_value = None

        reader = TranscriptReader(session_manager)
        result = await reader.get_messages("sess-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_role_filter_applied(self, tmp_path: Path):
        archive_dir = tmp_path / "archives"
        external_id = "ext-filter"

        lines = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        ]
        _write_gzip_archive(archive_dir, external_id, lines)

        session = MagicMock()
        session.external_id = external_id
        session.source = "claude"
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))
        result = await reader.get_messages("sess-1", role="user")

        for msg in result:
            assert msg["role"] == "user"

    @pytest.mark.asyncio
    async def test_pagination_applied(self, tmp_path: Path):
        archive_dir = tmp_path / "archives"
        external_id = "ext-page"

        lines = [
            {"type": "user", "message": {"role": "user", "content": f"msg {i}"}} for i in range(10)
        ]
        _write_gzip_archive(archive_dir, external_id, lines)

        session = MagicMock()
        session.external_id = external_id
        session.source = "claude"
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))
        result = await reader.get_messages("sess-1", limit=3, offset=2)

        assert len(result) == 3


class TestTranscriptReaderJsonlFallback:
    """TranscriptReader reads from live JSONL when available."""

    @pytest.mark.asyncio
    async def test_falls_back_to_jsonl(self, tmp_path: Path):
        transcript_path = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        ]
        _write_jsonl_file(transcript_path, lines)

        session = MagicMock()
        session.external_id = "no-archive"
        session.source = "claude"
        session.transcript_path = str(transcript_path)

        session_manager = MagicMock()
        session_manager.get.return_value = session

        archive_dir = tmp_path / "empty-archives"
        archive_dir.mkdir()

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))
        result = await reader.get_messages("sess-1", limit=50)

        assert len(result) > 0
        for msg in result:
            assert msg["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_count_falls_back_to_jsonl(self, tmp_path: Path):
        transcript_path = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        ]
        _write_jsonl_file(transcript_path, lines)

        session = MagicMock()
        session.external_id = "no-archive"
        session.source = "claude"
        session.transcript_path = str(transcript_path)

        session_manager = MagicMock()
        session_manager.get.return_value = session

        archive_dir = tmp_path / "empty-archives"
        archive_dir.mkdir()

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))
        count = await reader.count_messages("sess-1")

        assert count > 0

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_jsonl(self, tmp_path: Path):
        session = MagicMock()
        session.external_id = "no-archive"
        session.source = "claude"
        session.transcript_path = "/nonexistent/path.jsonl"

        session_manager = MagicMock()
        session_manager.get.return_value = session

        archive_dir = tmp_path / "empty-archives"
        archive_dir.mkdir()

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))
        result = await reader.get_messages("sess-1")

        assert result == []

    @pytest.mark.asyncio
    async def test_role_filter_applied(self, tmp_path: Path):
        transcript_path = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        ]
        _write_jsonl_file(transcript_path, lines)

        session = MagicMock()
        session.external_id = "no-archive"
        session.source = "claude"
        session.transcript_path = str(transcript_path)

        session_manager = MagicMock()
        session_manager.get.return_value = session

        archive_dir = tmp_path / "empty-archives"
        archive_dir.mkdir()

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))
        result = await reader.get_messages("sess-1", role="user")

        for msg in result:
            assert msg["role"] == "user"

    @pytest.mark.asyncio
    async def test_pagination_applied(self, tmp_path: Path):
        transcript_path = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": f"msg {i}"}} for i in range(10)
        ]
        _write_jsonl_file(transcript_path, lines)

        session = MagicMock()
        session.external_id = "no-archive"
        session.source = "claude"
        session.transcript_path = str(transcript_path)

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager, archive_dir=str(tmp_path))
        result = await reader.get_messages("sess-1", limit=3, offset=2)

        assert len(result) == 3


class TestTranscriptReaderRendered:
    """Tests for the get_rendered_messages method."""

    @pytest.mark.asyncio
    async def test_get_rendered_messages_jsonl(self, tmp_path: Path):
        transcript_path = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        ]
        _write_jsonl_file(transcript_path, lines)

        session = MagicMock()
        session.external_id = "no-archive"
        session.source = "claude"
        session.transcript_path = str(transcript_path)

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)

        result = await reader.get_rendered_messages("sess-1")

        assert len(result) == 2
        assert isinstance(result[0], RenderedMessage)
        assert result[0].role == "user"
        assert result[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_get_rendered_messages_gzip(self, tmp_path: Path):
        archive_dir = tmp_path / "archives"
        external_id = "ext-123"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        ]
        _write_gzip_archive(archive_dir, external_id, lines)

        session = MagicMock()
        session.external_id = external_id
        session.source = "claude"
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))

        result = await reader.get_rendered_messages("sess-1")

        assert len(result) == 2
        assert isinstance(result[0], RenderedMessage)

    @pytest.mark.asyncio
    async def test_get_rendered_messages_pagination(self, tmp_path: Path):
        transcript_path = tmp_path / "transcript.jsonl"
        lines = []
        for i in range(10):
            lines.append({"type": "user", "message": {"role": "user", "content": f"msg {i}"}})
        _write_jsonl_file(transcript_path, lines)

        session = MagicMock()
        session.external_id = "no-archive"
        session.source = "claude"
        session.transcript_path = str(transcript_path)

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)

        result = await reader.get_rendered_messages("sess-1", limit=3, offset=2)

        assert len(result) == 3
        assert "msg 2" in result[0].content
        assert "msg 4" in result[2].content

    @pytest.mark.asyncio
    async def test_get_rendered_messages_truncated_gzip(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archives"
        external_id = "ext-truncated"
        archive_dir.mkdir(parents=True, exist_ok=True)
        path = archive_dir / f"{external_id}.jsonl.gz"

        valid_line = (
            json.dumps({"type": "user", "message": {"role": "user", "content": "valid"}}) + "\n"
        )
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(valid_line)

        with open(path, "ab") as f:
            f.write(b"\x00\x01\x02\x03" * 10)

        session = MagicMock()
        session.external_id = external_id
        session.source = "claude"
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        clear_archive_cache()
        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))

        result = await reader.get_rendered_messages("sess-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_rendered_messages_empty_session(self):
        session_manager = MagicMock()
        session_manager.get.return_value = None

        reader = TranscriptReader(session_manager)
        result = await reader.get_rendered_messages("empty-session")
        assert result == []

    @pytest.mark.asyncio
    async def test_sniffs_codex_source_from_mismatched_live_jsonl(self, tmp_path: Path):
        transcript_path = tmp_path / "rollout-2026-04-13T10-00-00Z-ext-abc.jsonl"
        lines = [
            _make_codex_message("user", "hello from codex", "2026-04-13T10:00:00Z"),
            _make_codex_message("assistant", "codex reply", "2026-04-13T10:00:01Z"),
        ]
        _write_jsonl_file(transcript_path, lines)

        session = MagicMock()
        session.external_id = "ext-abc"
        session.source = "claude"
        session.transcript_path = str(transcript_path)

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)

        rendered = await reader.get_rendered_messages("sess-1")
        count = await reader.count_messages("sess-1")

        assert len(rendered) == 2
        assert rendered[0].role == "user"
        assert "hello from codex" in rendered[0].content
        assert count == 2

    @pytest.mark.asyncio
    async def test_sniffs_codex_source_from_mismatched_archive(self, tmp_path: Path):
        archive_dir = tmp_path / "archives"
        external_id = "ext-codex-archive"
        lines = [
            _make_codex_message("user", "archived user", "2026-04-13T10:00:00Z"),
            _make_codex_message("assistant", "archived assistant", "2026-04-13T10:00:01Z"),
        ]
        _write_gzip_archive(archive_dir, external_id, lines)

        session = MagicMock()
        session.external_id = external_id
        session.source = "claude"
        session.transcript_path = "/Users/test/.codex/sessions/2026/04/13/rollout-ext.jsonl"

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))

        rendered = await reader.get_rendered_messages("sess-1")
        count = await reader.count_messages("sess-1")

        assert len(rendered) == 2
        assert rendered[1].role == "assistant"
        assert "archived assistant" in rendered[1].content
        assert count == 2

    @pytest.mark.asyncio
    async def test_rederives_qwen_transcript_from_projects_layout(self, tmp_path: Path):
        external_id = "ext-qwen-123"
        transcript_path = (
            tmp_path / ".qwen" / "projects" / "project-slug" / "chats" / f"{external_id}.jsonl"
        )
        _write_jsonl_file(
            transcript_path,
            [
                {
                    "type": "user",
                    "content": "hello from qwen",
                },
                {
                    "type": "model",
                    "content": "qwen reply",
                },
            ],
        )

        session = MagicMock()
        session.external_id = external_id
        session.source = "qwen"
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)

        with patch.object(Path, "home", return_value=tmp_path):
            rendered = await reader.get_rendered_messages("sess-1")

        assert len(rendered) == 2
        assert "hello from qwen" in rendered[0].content
        assert session_manager.update.call_count >= 1
        assert session_manager.update.call_args_list[-1] == (
            ("sess-1",),
            {"transcript_path": str(transcript_path)},
        )

    @pytest.mark.asyncio
    async def test_rederives_transcript_path_in_thread(self) -> None:
        session = MagicMock()
        session.external_id = "ext-thread"
        session.source = "qwen"
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        async def run_in_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        to_thread = AsyncMock(side_effect=run_in_thread)
        reader = TranscriptReader(session_manager)

        with (
            patch(
                "gobby.sessions.transcript_reader._find_transcript_on_disk",
                return_value="/tmp/derived.jsonl",
            ) as find_transcript,
            patch("gobby.sessions.transcript_reader.asyncio.to_thread", new=to_thread),
        ):
            result = await reader._ensure_transcript_path(
                "sess-1",
                session,
                "qwen",
                None,
            )

        assert result == "/tmp/derived.jsonl"
        find_transcript.assert_called_once_with("qwen", "ext-thread")
        assert to_thread.await_args_list[0].args == (
            find_transcript,
            "qwen",
            "ext-thread",
        )
        assert to_thread.await_args_list[1].args == (session_manager.update, "sess-1")
        assert to_thread.await_args_list[1].kwargs == {"transcript_path": "/tmp/derived.jsonl"}

    @pytest.mark.asyncio
    async def test_reports_unparseable_transcript_status(self, tmp_path: Path):
        transcript_path = tmp_path / "mystery.jsonl"
        _write_jsonl_file(
            transcript_path,
            [{"weird": "shape"}, {"still": "unknown"}],
        )

        session = MagicMock()
        session.external_id = "mystery"
        session.source = "claude"
        session.transcript_path = str(transcript_path)

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)
        status = await reader.get_transcript_status("sess-1")

        assert status["availability"] == "live"
        assert status["content_state"] == "unparseable"
        assert status["raw_record_count"] == 2
        assert status["parsed_message_count"] == 0


class TestTranscriptReaderGeminiJSON:
    """TranscriptReader handles Gemini native JSON session files."""

    @pytest.mark.asyncio
    async def test_read_gemini_json_get_messages(self, tmp_path: Path):
        """get_messages works with Gemini JSON session files."""
        json_path = tmp_path / "session-2025-03-23T10-00-00-abc12345.json"
        gemini_session = {
            "sessionId": "abc12345-full-uuid",
            "messages": [
                {"type": "user", "content": "hello gemini", "timestamp": "2025-03-23T10:00:00Z"},
                {"type": "gemini", "content": "hi there!", "timestamp": "2025-03-23T10:00:01Z"},
            ],
        }
        json_path.write_text(json.dumps(gemini_session))

        session = MagicMock()
        session.source = "gemini"
        session.transcript_path = str(json_path)
        session.external_id = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)
        result = await reader.get_messages("sess-1", limit=50)

        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hello gemini"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "hi there!"

    @pytest.mark.asyncio
    async def test_read_gemini_json_rendered(self, tmp_path: Path):
        """get_rendered_messages works with Gemini JSON session files."""
        json_path = tmp_path / "session-2025-03-23T10-00-00-abc12345.json"
        gemini_session = {
            "sessionId": "abc12345-full-uuid",
            "messages": [
                {"type": "user", "content": "what is 2+2?", "timestamp": "2025-03-23T10:00:00Z"},
                {"type": "gemini", "content": "4", "timestamp": "2025-03-23T10:00:01Z"},
            ],
        }
        json_path.write_text(json.dumps(gemini_session))

        session = MagicMock()
        session.source = "gemini"
        session.transcript_path = str(json_path)
        session.external_id = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)
        result = await reader.get_rendered_messages("sess-1")

        assert len(result) == 2
        assert isinstance(result[0], RenderedMessage)
        assert result[0].role == "user"
        assert "2+2" in result[0].content
        assert result[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_count_gemini_json_messages(self, tmp_path: Path):
        """count_messages works with Gemini JSON session files."""
        json_path = tmp_path / "session-test.json"
        gemini_session = {
            "sessionId": "test-uuid",
            "messages": [
                {"type": "user", "content": "msg1", "timestamp": "2025-03-23T10:00:00Z"},
                {"type": "gemini", "content": "reply1", "timestamp": "2025-03-23T10:00:01Z"},
                {"type": "user", "content": "msg2", "timestamp": "2025-03-23T10:00:02Z"},
            ],
        }
        json_path.write_text(json.dumps(gemini_session))

        session = MagicMock()
        session.source = "gemini"
        session.transcript_path = str(json_path)
        session.external_id = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)
        count = await reader.count_messages("sess-1")

        assert count == 3

    @pytest.mark.asyncio
    async def test_gemini_json_with_tool_calls(self, tmp_path: Path):
        """Gemini JSON with embedded toolCalls parses correctly."""
        json_path = tmp_path / "session-tools.json"
        gemini_session = {
            "sessionId": "tools-uuid",
            "messages": [
                {"type": "user", "content": "list files", "timestamp": "2025-03-23T10:00:00Z"},
                {
                    "type": "gemini",
                    "content": "Let me check.",
                    "timestamp": "2025-03-23T10:00:01Z",
                    "toolCalls": [
                        {"name": "ReadFile", "args": {"path": "/tmp/test.py"}},
                    ],
                },
            ],
        }
        json_path.write_text(json.dumps(gemini_session))

        session = MagicMock()
        session.source = "gemini"
        session.transcript_path = str(json_path)
        session.external_id = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)
        result = await reader.get_messages("sess-1", limit=50)

        # user + assistant text + tool_use = at least 3 messages
        assert len(result) >= 3
        tool_msgs = [m for m in result if m["content_type"] == "tool_use"]
        assert len(tool_msgs) >= 1
        assert tool_msgs[0]["tool_name"] == "ReadFile"

    @pytest.mark.asyncio
    async def test_jsonl_still_works_for_gemini(self, tmp_path: Path):
        """Gemini source with .jsonl file still uses JSONL parsing (regression test)."""
        jsonl_path = tmp_path / "transcript.jsonl"
        lines = [
            {
                "type": "message",
                "role": "user",
                "content": "hello",
                "timestamp": "2025-03-23T10:00:00Z",
            },
            {
                "type": "message",
                "role": "model",
                "content": "hi",
                "timestamp": "2025-03-23T10:00:01Z",
            },
        ]
        _write_jsonl_file(jsonl_path, lines)

        session = MagicMock()
        session.source = "gemini"
        session.transcript_path = str(jsonl_path)
        session.external_id = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)
        result = await reader.get_messages("sess-1", limit=50)

        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
