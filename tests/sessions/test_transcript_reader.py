"""Tests for TranscriptReader — JSONL + gzip archive read layer."""

import gzip
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.transcript_index import clear_index_cache
from gobby.sessions.transcript_io import TranscriptTooLargeError
from gobby.sessions.transcript_paths import _find_transcript_on_disk, _is_recent_file
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
    """Clear LRU caches before/after each test."""
    clear_archive_cache()
    clear_index_cache()
    yield
    clear_archive_cache()
    clear_index_cache()


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


def test_transcript_scan_respects_file_age_for_claude(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / ".claude" / "projects" / "project" / "ext-old.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("{}\n", encoding="utf-8")
    old_mtime = time() - (3 * 24 * 60 * 60)
    os.utime(target, (old_mtime, old_mtime))

    assert _find_transcript_on_disk("claude", "ext-old", max_days=2) is None
    assert _find_transcript_on_disk("claude", "ext-old", max_days=4) == str(target)


def test_transcript_scan_ignores_os_errors_during_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True)

    with patch.object(Path, "iterdir", side_effect=OSError("permission denied")):
        assert _find_transcript_on_disk("claude", "ext-any", max_days=7) is None


def test_codex_transcript_scan_treats_external_id_as_literal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    day_dir = tmp_path / ".codex" / "sessions" / "2026" / "05" / "01"
    day_dir.mkdir(parents=True)
    literal = day_dir / "session-ext[abc].jsonl"
    wildcard_match = day_dir / "session-exta.jsonl"
    literal.write_text("{}\n", encoding="utf-8")
    wildcard_match.write_text("{}\n", encoding="utf-8")

    assert _find_transcript_on_disk("codex", "ext[abc]", max_days=1) == str(literal)


def test_gemini_transcript_scan_requires_full_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    chats_dir = tmp_path / ".gemini" / "tmp" / "project" / "chats"
    chats_dir.mkdir(parents=True)
    target = chats_dir / "session-2026-short.json"
    target.write_text("[]", encoding="utf-8")

    assert _find_transcript_on_disk("gemini", "short", max_days=1) is None


def test_is_recent_file_rejects_non_positive_max_days(tmp_path: Path) -> None:
    target = tmp_path / "session.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="max_days must be positive"):
        _is_recent_file(target, 0)


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

    @pytest.mark.asyncio
    async def test_get_messages_skips_missing_live_file_and_reads_archive(
        self,
        tmp_path: Path,
    ) -> None:
        archive_dir = tmp_path / "archives"
        external_id = "ext-missing-live"
        _write_gzip_archive(
            archive_dir,
            external_id,
            [{"type": "user", "message": {"role": "user", "content": "archive"}}],
        )

        session = MagicMock()
        session.external_id = external_id
        session.source = "claude"
        session.transcript_path = str(tmp_path / "missing.jsonl")

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))
        # transcript_path points at a missing file, so the live read is skipped
        # (path resolves to None) and the archive is read instead.
        result = await reader.get_messages("sess-1", limit=50)

        assert len(result) == 1
        assert result[0]["content"] == "archive"


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
    async def test_get_rendered_messages_limit_none_returns_all_after_offset(
        self,
        tmp_path: Path,
    ) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": f"msg {i}"}} for i in range(5)
        ]
        _write_jsonl_file(transcript_path, lines)

        session = MagicMock()
        session.external_id = "no-archive"
        session.source = "claude"
        session.transcript_path = str(transcript_path)

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)

        result = await reader.get_rendered_messages("sess-1", limit=None, offset=2)

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


def _gemini_json_reader(
    tmp_path: Path,
    filename: str,
    messages: list[dict[str, object]],
    *,
    session_id: str = "gemini-session-uuid",
) -> TranscriptReader:
    json_path = tmp_path / filename
    json_path.write_text(
        json.dumps({"sessionId": session_id, "messages": messages}),
        encoding="utf-8",
    )

    session = MagicMock()
    session.source = "gemini"
    session.transcript_path = str(json_path)
    session.external_id = None

    session_manager = MagicMock()
    session_manager.get.return_value = session
    return TranscriptReader(session_manager)


class TestTranscriptReaderGeminiJSON:
    """TranscriptReader handles Gemini native JSON session files."""

    @pytest.mark.asyncio
    async def test_read_gemini_json_get_messages(self, tmp_path: Path):
        """get_messages works with Gemini JSON session files."""
        reader = _gemini_json_reader(
            tmp_path,
            "session-2025-03-23T10-00-00-abc12345.json",
            [
                {"type": "user", "content": "hello gemini", "timestamp": "2025-03-23T10:00:00Z"},
                {"type": "gemini", "content": "hi there!", "timestamp": "2025-03-23T10:00:01Z"},
            ],
            session_id="abc12345-full-uuid",
        )
        result = await reader.get_messages("sess-1", limit=50)

        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hello gemini"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "hi there!"

    @pytest.mark.asyncio
    async def test_read_gemini_json_rendered(self, tmp_path: Path):
        """get_rendered_messages works with Gemini JSON session files."""
        reader = _gemini_json_reader(
            tmp_path,
            "session-2025-03-23T10-00-00-abc12345.json",
            [
                {"type": "user", "content": "what is 2+2?", "timestamp": "2025-03-23T10:00:00Z"},
                {"type": "gemini", "content": "4", "timestamp": "2025-03-23T10:00:01Z"},
            ],
            session_id="abc12345-full-uuid",
        )
        result = await reader.get_rendered_messages("sess-1")

        assert len(result) == 2
        assert isinstance(result[0], RenderedMessage)
        assert result[0].role == "user"
        assert "2+2" in result[0].content
        assert result[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_count_gemini_json_messages(self, tmp_path: Path):
        """count_messages works with Gemini JSON session files."""
        reader = _gemini_json_reader(
            tmp_path,
            "session-test.json",
            [
                {"type": "user", "content": "msg1", "timestamp": "2025-03-23T10:00:00Z"},
                {"type": "gemini", "content": "reply1", "timestamp": "2025-03-23T10:00:01Z"},
                {"type": "user", "content": "msg2", "timestamp": "2025-03-23T10:00:02Z"},
            ],
            session_id="test-uuid",
        )
        count = await reader.count_messages("sess-1")

        assert count == 3

    @pytest.mark.asyncio
    async def test_gemini_json_with_tool_calls(self, tmp_path: Path):
        """Gemini JSON with embedded toolCalls parses correctly."""
        reader = _gemini_json_reader(
            tmp_path,
            "session-tools.json",
            [
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
            session_id="tools-uuid",
        )
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


def _jsonl_reader_with_user_msgs(tmp_path: Path, count: int) -> TranscriptReader:
    """A reader over a live JSONL transcript of ``count`` distinct user groups."""
    transcript_path = tmp_path / "transcript.jsonl"
    _write_jsonl_file(
        transcript_path,
        [
            {"type": "user", "message": {"role": "user", "content": f"msg {i}"}}
            for i in range(count)
        ],
    )
    session = MagicMock()
    session.external_id = "no-archive"
    session.source = "claude"
    session.transcript_path = str(transcript_path)
    session_manager = MagicMock()
    session_manager.get.return_value = session
    return TranscriptReader(session_manager)


class TestTranscriptReaderWindowed:
    """Windowed rendered reads: tail/head ordering, paging, and native guard."""

    @pytest.mark.asyncio
    async def test_get_messages_limit_zero_returns_empty(self, tmp_path: Path) -> None:
        reader = _jsonl_reader_with_user_msgs(tmp_path, 3)

        assert await reader.get_messages("sess-1", limit=0) == []

    @pytest.mark.asyncio
    async def test_window_limit_zero_returns_empty_with_total(self, tmp_path: Path) -> None:
        reader = _jsonl_reader_with_user_msgs(tmp_path, 3)

        result = await reader.get_rendered_window("sess-1", limit=0, offset=0, order="tail")

        assert result.groups == []
        assert result.returned_count == 0
        assert result.total_groups == 3

    @pytest.mark.asyncio
    async def test_window_tail_returns_newest_slice(self, tmp_path: Path) -> None:
        reader = _jsonl_reader_with_user_msgs(tmp_path, 5)

        result = await reader.get_rendered_window("sess-1", limit=2, offset=0, order="tail")

        # Oldest-first within the page, newest slice of the transcript.
        assert result.returned_count == 2
        assert result.total_groups == 5
        assert result.parsed_message_count == 5
        assert "msg 3" in result.groups[0].content
        assert "msg 4" in result.groups[1].content

    @pytest.mark.asyncio
    async def test_window_tail_offset_pages_older(self, tmp_path: Path) -> None:
        reader = _jsonl_reader_with_user_msgs(tmp_path, 5)

        page0 = await reader.get_rendered_window("sess-1", limit=2, offset=0, order="tail")
        page1 = await reader.get_rendered_window(
            "sess-1", limit=2, offset=page0.returned_count, order="tail"
        )

        assert [g.content for g in page1.groups] == ["msg 1", "msg 2"]

    @pytest.mark.asyncio
    async def test_window_head_matches_chronological(self, tmp_path: Path) -> None:
        reader = _jsonl_reader_with_user_msgs(tmp_path, 4)

        result = await reader.get_rendered_window("sess-1", limit=3, offset=1, order="head")

        assert [g.content for g in result.groups] == ["msg 1", "msg 2", "msg 3"]

    @pytest.mark.asyncio
    async def test_iter_rendered_windows_tiles_full_render(self, tmp_path: Path) -> None:
        reader = _jsonl_reader_with_user_msgs(tmp_path, 5)

        pages = [page async for page in reader.iter_rendered_windows("sess-1", page=2)]

        flat = [g.content for page in pages for g in page]
        assert flat == [f"msg {i}" for i in range(5)]
        # Tiling composes without gaps/overlaps across page boundaries.
        assert sum(len(p) for p in pages) == 5

    @pytest.mark.asyncio
    async def test_get_rendered_messages_clamps_unbounded_limit(self, tmp_path: Path) -> None:
        reader = _jsonl_reader_with_user_msgs(tmp_path, 3)

        # limit=None must not reintroduce a full unbounded render; it is clamped.
        result = await reader.get_rendered_messages("sess-1", limit=None, offset=0)

        assert [g.content for g in result] == ["msg 0", "msg 1", "msg 2"]

    @pytest.mark.asyncio
    async def test_native_json_size_guard_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        json_path = tmp_path / "session-big.json"
        json_path.write_text(
            json.dumps(
                {
                    "sessionId": "big-uuid",
                    "messages": [
                        {"type": "user", "content": "hi", "timestamp": "2025-03-23T10:00:00Z"},
                    ],
                }
            )
        )
        session = MagicMock()
        session.source = "gemini"
        session.transcript_path = str(json_path)
        session.external_id = None
        session_manager = MagicMock()
        session_manager.get.return_value = session

        monkeypatch.setattr("gobby.sessions.transcript_reader.NATIVE_JSON_MAX_BYTES", 1)
        reader = TranscriptReader(session_manager)

        with pytest.raises(TranscriptTooLargeError):
            await reader.get_rendered_window("sess-1", limit=50, offset=0, order="tail")

    @pytest.mark.asyncio
    async def test_native_json_get_messages_size_guard_returns_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        json_path = tmp_path / "session-big.json"
        json_path.write_text(
            json.dumps(
                {
                    "sessionId": "big-uuid",
                    "messages": [
                        {"type": "user", "content": "hi", "timestamp": "2025-03-23T10:00:00Z"},
                    ],
                }
            )
        )
        session = MagicMock()
        session.source = "gemini"
        session.transcript_path = str(json_path)
        session.external_id = None
        session_manager = MagicMock()
        session_manager.get.return_value = session

        monkeypatch.setattr("gobby.sessions.transcript_reader.NATIVE_JSON_MAX_BYTES", 1)
        reader = TranscriptReader(session_manager)

        assert await reader.get_messages("sess-1", limit=50) == []

    @pytest.mark.asyncio
    async def test_count_native_json_size_guard_returns_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        json_path = tmp_path / "session-big.json"
        json_path.write_text(
            json.dumps(
                {
                    "sessionId": "big-uuid",
                    "messages": [
                        {"type": "user", "content": "hi", "timestamp": "2025-03-23T10:00:00Z"},
                    ],
                }
            )
        )
        session = MagicMock()
        session.source = "gemini"
        session.transcript_path = str(json_path)
        session.external_id = None
        session_manager = MagicMock()
        session_manager.get.return_value = session

        monkeypatch.setattr("gobby.sessions.transcript_reader.NATIVE_JSON_MAX_BYTES", 1)
        reader = TranscriptReader(session_manager)

        assert await reader.count_messages("sess-1") == 0

    @pytest.mark.asyncio
    async def test_activity_counts_native_json_size_guard_returns_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        json_path = tmp_path / "session-big.json"
        json_path.write_text(
            json.dumps(
                {
                    "sessionId": "big-uuid",
                    "messages": [
                        {"type": "user", "content": "hi", "timestamp": "2025-03-23T10:00:00Z"},
                    ],
                }
            )
        )
        session = MagicMock()
        session.source = "gemini"
        session.transcript_path = str(json_path)
        session.external_id = None
        session_manager = MagicMock()
        session_manager.get.return_value = session

        monkeypatch.setattr("gobby.sessions.transcript_reader.NATIVE_JSON_MAX_BYTES", 1)
        reader = TranscriptReader(session_manager)

        assert await reader.get_activity_counts("sess-1") == {
            "message_count": 0,
            "turn_count": 0,
            "tool_call_count": 0,
        }
