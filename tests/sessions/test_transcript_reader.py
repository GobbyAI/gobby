"""Tests for TranscriptReader — JSONL + gzip archive read layer."""

import gzip
import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.gzip_seek_index import load_gzip_block_index
from gobby.sessions.transcript_index import TranscriptIndex, clear_index_cache, get_or_build_index
from gobby.sessions.transcript_paths import _is_recent_file, find_transcript_on_disk
from gobby.sessions.transcript_reader import (
    TranscriptReader,
    _collect_flat_from_file,
    clear_archive_cache,
)
from gobby.sessions.transcript_renderer import RenderedMessage
from gobby.sessions.transcripts.base import RawLine

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


# Helper to write a plain JSONL file (not gzipped)
def _write_jsonl_file(path: Path, lines: list[dict[str, Any]]) -> Path:
    """Write JSONL lines to a plain file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def _make_codex_message(role: str, text: str, ts: str) -> dict[str, Any]:
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
def _clear_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear LRU caches before/after each test."""

    def require_local_ownership(session: Any) -> str:
        session.machine_id = LOCAL_MACHINE_ID
        return LOCAL_MACHINE_ID

    monkeypatch.setattr(
        "gobby.sessions.transcript_reader.require_local_session_ownership",
        require_local_ownership,
    )
    clear_archive_cache()
    clear_index_cache()
    yield
    clear_archive_cache()
    clear_index_cache()


def _make_msg_dict(index: int, role: str = "assistant", content: str = "hi") -> dict[str, Any]:
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

    assert (
        find_transcript_on_disk(
            "codex",
            "ext-abc",
            max_days=2,
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
        )
        is None
    )
    assert find_transcript_on_disk(
        "codex",
        "ext-abc",
        max_days=3,
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
    ) == str(target)


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

    assert (
        find_transcript_on_disk(
            "claude",
            "ext-old",
            max_days=2,
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
        )
        is None
    )
    assert find_transcript_on_disk(
        "claude",
        "ext-old",
        max_days=4,
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
    ) == str(target)


def test_transcript_scan_ignores_os_errors_during_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True)

    with patch.object(Path, "iterdir", side_effect=OSError("permission denied")):
        assert (
            find_transcript_on_disk(
                "claude",
                "ext-any",
                max_days=7,
                owner_machine_id=LOCAL_MACHINE_ID,
                local_machine_id=LOCAL_MACHINE_ID,
            )
            is None
        )


def test_codex_transcript_scan_treats_external_id_as_literal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    day_dir = tmp_path / ".codex" / "sessions" / "2026" / "05" / "01"
    day_dir.mkdir(parents=True)
    literal = day_dir / "rollout-session-ext[abc].jsonl"
    wildcard_match = day_dir / "rollout-session-exta.jsonl"
    literal.write_text("{}\n", encoding="utf-8")
    wildcard_match.write_text("{}\n", encoding="utf-8")

    assert find_transcript_on_disk(
        "codex",
        "ext[abc]",
        max_days=1,
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
    ) == str(literal)


def test_codex_transcript_scan_ignores_sidecars_and_non_rollout_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    day_dir = tmp_path / ".codex" / "sessions" / "2026" / "05" / "01"
    day_dir.mkdir(parents=True)
    external_id = "ext-abc"
    non_rollout = day_dir / f"session-{external_id}.jsonl"
    adjacent_sidecar = day_dir / f"rollout-session-{external_id}.jsonl.gobby-index.json"
    non_rollout.write_text("{}\n", encoding="utf-8")
    adjacent_sidecar.write_text("{}\n", encoding="utf-8")

    assert (
        find_transcript_on_disk(
            "codex",
            external_id,
            max_days=1,
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
        )
        is None
    )

    rollout = day_dir / f"rollout-session-{external_id}.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    assert find_transcript_on_disk(
        "codex",
        external_id,
        max_days=1,
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
    ) == str(rollout)


def test_agy_disk_fallback_uses_transcript_full_not_truncated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = (
        tmp_path
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / "conv-1"
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    target.parent.mkdir(parents=True)
    target.write_text("{}\n", encoding="utf-8")
    (target.parent / "transcript.jsonl").write_text("{}\n", encoding="utf-8")

    hook_path = find_transcript_on_disk(
        "agy",
        "conv-1",
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
        caller_context="hook",
    )
    recovery_path = find_transcript_on_disk(
        "agy",
        "conv-1",
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
        caller_context="recovery",
    )
    assert hook_path == str(target)
    assert recovery_path == str(target)


def test_hook_context_does_not_traverse_claude_projects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / ".claude" / "projects" / "project" / "ext-1.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("{}\n", encoding="utf-8")

    assert (
        find_transcript_on_disk(
            "claude",
            "ext-1",
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
            caller_context="hook",
        )
        is None
    )
    assert find_transcript_on_disk(
        "claude",
        "ext-1",
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
        caller_context="recovery",
    ) == str(target)


def test_is_recent_file_rejects_non_positive_max_days(tmp_path: Path) -> None:
    target = tmp_path / "session.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="max_days must be positive"):
        _is_recent_file(target, 0)


def _write_gzip_archive(archive_dir: Path, external_id: str, lines: list[dict[str, Any]]) -> Path:
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
    async def test_falls_back_to_gzip(self, tmp_path: Path) -> None:
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
    async def test_count_falls_back_to_gzip(self, tmp_path: Path) -> None:
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
    async def test_returns_empty_when_no_archive(self, tmp_path: Path) -> None:
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
    async def test_returns_empty_when_no_external_id(self) -> None:
        session = MagicMock()
        session.external_id = None
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session

        reader = TranscriptReader(session_manager)
        result = await reader.get_messages("sess-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_session_not_found(self) -> None:
        session_manager = MagicMock()
        session_manager.get.return_value = None

        reader = TranscriptReader(session_manager)
        result = await reader.get_messages("sess-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_role_filter_applied(self, tmp_path: Path) -> None:
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
    async def test_pagination_applied(self, tmp_path: Path) -> None:
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
    async def test_falls_back_to_jsonl(self, tmp_path: Path) -> None:
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
    async def test_count_falls_back_to_jsonl(self, tmp_path: Path) -> None:
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
    async def test_returns_empty_when_no_jsonl(self, tmp_path: Path) -> None:
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
    async def test_role_filter_applied(self, tmp_path: Path) -> None:
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
    async def test_pagination_applied(self, tmp_path: Path) -> None:
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
    async def test_get_rendered_messages_jsonl(self, tmp_path: Path) -> None:
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
    async def test_get_rendered_messages_gzip_replaces_invalid_utf8(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archives"
        archive_dir.mkdir()
        archive_path = archive_dir / "invalid-utf8.jsonl.gz"
        records = [
            {"type": "user", "message": {"role": "user", "content": "before"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "after"}},
        ]
        with gzip.open(archive_path, "wb") as handle:
            handle.write(json.dumps(records[0]).encode() + b"\n")
            handle.write(b'{"invalid": "\xff"}\n')
            handle.write(json.dumps(records[1]).encode() + b"\n")

        session = MagicMock()
        session.external_id = "invalid-utf8"
        session.source = "claude"
        session.transcript_path = str(tmp_path / "missing.jsonl")
        session_manager = MagicMock()
        session_manager.get.return_value = session
        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))

        result = await reader.get_rendered_messages("sess-1")

        assert [message.content for message in result] == ["before", "after"]

    @pytest.mark.asyncio
    async def test_get_rendered_messages_gzip(self, tmp_path: Path) -> None:
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
    async def test_get_rendered_messages_pagination(self, tmp_path: Path) -> None:
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
    async def test_get_rendered_messages_empty_session(self) -> None:
        session_manager = MagicMock()
        session_manager.get.return_value = None

        reader = TranscriptReader(session_manager)
        result = await reader.get_rendered_messages("empty-session")
        assert result == []

    @pytest.mark.asyncio
    async def test_sniffs_codex_source_from_mismatched_live_jsonl(self, tmp_path: Path) -> None:
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
    async def test_sniffs_codex_source_from_mismatched_archive(self, tmp_path: Path) -> None:
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
    async def test_rederives_qwen_transcript_from_projects_layout(self, tmp_path: Path) -> None:
        external_id = "ext-qwen-123"
        transcript_path = (
            tmp_path / ".qwen" / "projects" / "project-slug" / "chats" / f"{external_id}.jsonl"
        )
        _write_jsonl_file(
            transcript_path,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "parts": [{"text": "hello from qwen"}],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "model",
                        "parts": [{"text": "qwen reply"}],
                    },
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

        async def run_in_thread(
            func: Callable[..., object], *args: object, **kwargs: object
        ) -> object:
            return func(*args, **kwargs)

        to_thread = AsyncMock(side_effect=run_in_thread)
        reader = TranscriptReader(session_manager)

        with (
            patch(
                "gobby.sessions.transcript_reader.find_transcript_on_disk",
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
        find_transcript.assert_called_once_with(
            "qwen",
            "ext-thread",
            owner_machine_id=LOCAL_MACHINE_ID,
            local_machine_id=LOCAL_MACHINE_ID,
            caller_context="recovery",
        )
        assert to_thread.await_args_list[0].args == (
            find_transcript,
            "qwen",
            "ext-thread",
        )
        assert to_thread.await_args_list[1].args == (session_manager.update, "sess-1")
        assert to_thread.await_args_list[1].kwargs == {"transcript_path": "/tmp/derived.jsonl"}

    @pytest.mark.asyncio
    async def test_reports_unparseable_transcript_status(self, tmp_path: Path) -> None:
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


def _codex_reader_with_roles(tmp_path: Path, count: int) -> tuple[TranscriptReader, Path]:
    transcript_path = tmp_path / "codex-rollout.jsonl"
    _write_jsonl_file(
        transcript_path,
        [
            _make_codex_message(
                "user" if i % 3 == 0 else "assistant",
                f"msg {i}",
                f"2025-03-23T10:{i // 60:02d}:{i % 60:02d}Z",
            )
            for i in range(count)
        ],
    )
    session = MagicMock()
    session.external_id = "no-archive"
    session.source = "codex"
    session.transcript_path = str(transcript_path)
    session_manager = MagicMock()
    session_manager.get.return_value = session
    return TranscriptReader(session_manager), transcript_path


class TestTranscriptReaderWindowed:
    """Windowed rendered reads: tail/head ordering, paging, and native guard."""

    @pytest.mark.asyncio
    async def test_get_messages_limit_zero_returns_empty(self, tmp_path: Path) -> None:
        reader = _jsonl_reader_with_user_msgs(tmp_path, 3)

        assert await reader.get_messages("sess-1", limit=0) == []

    @pytest.mark.asyncio
    async def test_get_messages_deep_window_matches_streaming_path(self, tmp_path: Path) -> None:
        reader, transcript_path = _codex_reader_with_roles(tmp_path, 260)
        offset = 137
        limit = 9
        expected = _collect_flat_from_file(
            str(transcript_path), "codex", "sess-1", offset + limit, None
        )[offset : offset + limit]

        result = await reader.get_messages("sess-1", limit=limit, offset=offset)

        assert result == expected

    @pytest.mark.asyncio
    async def test_get_messages_role_window_matches_streaming_path(self, tmp_path: Path) -> None:
        reader, transcript_path = _codex_reader_with_roles(tmp_path, 260)
        offset = 31
        limit = 7
        expected = _collect_flat_from_file(
            str(transcript_path), "codex", "sess-1", offset + limit, "assistant"
        )[offset : offset + limit]

        result = await reader.get_messages("sess-1", limit=limit, offset=offset, role="assistant")

        assert result == expected

    @pytest.mark.asyncio
    async def test_get_messages_deep_live_jsonl_starts_from_parsed_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader, transcript_path = _codex_reader_with_roles(tmp_path, 420)
        st = os.stat(transcript_path)
        await get_or_build_index(
            str(transcript_path),
            "codex",
            "sess-1",
            seek_mode="byte",
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
        )
        clear_index_cache()
        starts: list[tuple[int, int]] = []

        from gobby.sessions import transcript_reader as reader_module

        original = reader_module._iter_jsonl_raw_lines_from

        def capture_start(
            path: str, start_byte: int, start_line_no: int, size: int
        ) -> Iterator[RawLine]:
            starts.append((start_byte, start_line_no))
            return original(path, start_byte, start_line_no, size)

        def fail_streaming_fallback(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("windowed flat read should not use prefix streaming fallback")

        monkeypatch.setattr(reader_module, "_iter_jsonl_raw_lines_from", capture_start)
        monkeypatch.setattr(reader_module, "_collect_flat_from_file", fail_streaming_fallback)

        result = await reader.get_messages("sess-1", limit=3, offset=300)

        assert len(result) == 3
        assert starts
        assert starts[0][0] > 0
        assert starts[0][1] >= 128

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
    async def test_archived_window_reblocks_and_uses_gzip_block_index(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archives"
        external_id = "archived-window"
        archive_path = _write_gzip_archive(
            archive_dir,
            external_id,
            [
                {"type": "user", "message": {"role": "user", "content": f"msg {i}"}}
                for i in range(6)
            ],
        )
        session = MagicMock()
        session.external_id = external_id
        session.source = "claude"
        session.transcript_path = str(tmp_path / "missing.jsonl")
        session_manager = MagicMock()
        session_manager.get.return_value = session
        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))

        result = await reader.get_rendered_window("sess-1", limit=2, offset=0, order="tail")

        assert result.returned_count == 2
        assert result.total_groups == 6
        assert "msg 4" in result.groups[0].content
        assert "msg 5" in result.groups[1].content
        assert load_gzip_block_index(str(archive_path)) is not None

    @pytest.mark.asyncio
    async def test_archive_status_and_window_share_transcript_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive_dir = tmp_path / "archives"
        external_id = "shared-index"
        _write_gzip_archive(
            archive_dir,
            external_id,
            [
                {"type": "user", "message": {"role": "user", "content": f"msg {i}"}}
                for i in range(3)
            ],
        )
        session = MagicMock()
        session.external_id = external_id
        session.source = "claude"
        session.transcript_path = str(tmp_path / "missing.jsonl")
        session_manager = MagicMock()
        session_manager.get.return_value = session
        reader = TranscriptReader(session_manager, archive_dir=str(archive_dir))

        initial_window = await reader.get_rendered_window("sess-1", limit=1, offset=0)
        assert initial_window.total_groups == 3
        clear_index_cache()

        from gobby.sessions import transcript_index_sidecar

        persisted_modes: list[str] = []
        original_persist = transcript_index_sidecar.persist_index_sidecar

        def record_persist(path: str, index: TranscriptIndex) -> None:
            persisted_modes.append(index.seek_mode)
            original_persist(path, index)

        monkeypatch.setattr(transcript_index_sidecar, "persist_index_sidecar", record_persist)

        status = await reader.get_transcript_status("sess-1")
        window = await reader.get_rendered_window("sess-1", limit=1, offset=0)
        repeated_status = await reader.get_transcript_status("sess-1")

        assert status["parsed_message_count"] == 3
        assert window.total_groups == 3
        assert repeated_status["parsed_message_count"] == 3
        assert persisted_modes == []

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
    async def test_qwen_json_uses_indexed_windows_counts_and_status(self, tmp_path: Path) -> None:
        transcript = tmp_path / "qwen-session.json"
        fixture = (
            Path(__file__).parents[1]
            / "fixtures"
            / "transcripts"
            / "qwen"
            / "current_envelope.jsonl"
        )
        transcript.write_text(fixture.read_text())
        session = MagicMock()
        session.source = "qwen"
        session.transcript_path = str(transcript)
        session.external_id = None
        session_manager = MagicMock()
        session_manager.get.return_value = session
        reader = TranscriptReader(session_manager)

        rows = await reader.get_messages("sess-1", limit=3, offset=1)
        window = await reader.get_rendered_window("sess-1", limit=2, offset=0, order="head")
        count = await reader.count_messages("sess-1")
        activity = await reader.get_activity_counts("sess-1")
        status = await reader.get_transcript_status("sess-1")

        assert len(rows) == 3
        assert window.returned_count == 2
        assert count == 7
        assert activity["message_count"] == 7
        assert activity["tool_call_count"] == 1
        assert status["raw_record_count"] == 7
        assert status["parsed_message_count"] == 7
        assert status["content_state"] == "messages"
        assert status["detected_source"] == "qwen"
        assert status["source_mismatch"] is False
