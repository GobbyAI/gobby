"""Tests for WebSocket proxy attachment helpers."""

from __future__ import annotations

import binascii
import os
from pathlib import Path

import pytest

from gobby.servers.websocket import attachments as attachment_helpers
from gobby.servers.websocket.attachments import (
    _safe_path_part,
    cleanup_expired_proxy_attachments,
    cleanup_proxy_attachments_for_session,
    store_proxy_attachments,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../note.txt", "_note.txt"),
        ("...hidden", "hidden"),
        ("a/b\\c?.txt", "a_b_c_.txt"),
        ("\x00", "fallback"),
    ],
)
def test_safe_path_part_sanitizes_deterministically(raw: str, expected: str) -> None:
    assert _safe_path_part(raw, "fallback") == expected


def test_safe_path_part_truncates_preserving_extension() -> None:
    result = _safe_path_part(f"{'a' * 200}.txt", "fallback")

    assert result.endswith(".txt")
    assert len(result) <= attachment_helpers._SAFE_PATH_PART_MAX_LENGTH


@pytest.mark.asyncio
async def test_store_proxy_attachments_validates_all_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))

    with pytest.raises(ValueError) as exc_info:
        await store_proxy_attachments(
            "session-1",
            [
                {"name": "valid.txt", "base64": "aGVsbG8="},
                {"name": "invalid.txt", "base64": "!!!!"},
            ],
        )

    assert "invalid base64 content" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, binascii.Error)
    assert not (tmp_path / "attachments").exists()


@pytest.mark.asyncio
async def test_store_proxy_attachments_cleans_partial_file_after_write_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))

    def fail_write(target: Path, *_args: object) -> int:
        target.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(attachment_helpers, "_write_base64_attachment", fail_write)

    with pytest.raises(OSError, match="disk full"):
        await store_proxy_attachments(
            "session-1",
            [{"name": "one.txt", "base64": "MQ=="}],
        )

    assert not (tmp_path / "attachments").exists()


@pytest.mark.asyncio
async def test_store_proxy_attachments_rejects_too_many_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr(attachment_helpers, "MAX_PROXY_ATTACHMENT_COUNT", 1)

    with pytest.raises(ValueError, match="Too many attachments"):
        await store_proxy_attachments(
            "session-1",
            [
                {"name": "one.txt", "base64": "MQ=="},
                {"name": "two.txt", "base64": "Mg=="},
            ],
        )

    assert not (tmp_path / "attachments").exists()


@pytest.mark.asyncio
async def test_store_proxy_attachments_rejects_declared_total_size_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr(attachment_helpers, "MAX_PROXY_TOTAL_ATTACHMENT_BYTES", 5)

    limit = attachment_helpers.MAX_PROXY_TOTAL_ATTACHMENT_BYTES
    with pytest.raises(ValueError, match=rf"exceed {limit} bytes total"):
        await store_proxy_attachments(
            "session-1",
            [
                {"name": "one.txt", "base64": "MQ==", "size": 3},
                {"name": "two.txt", "base64": "Mg==", "size": 3},
            ],
        )

    assert not (tmp_path / "attachments").exists()


@pytest.mark.asyncio
async def test_store_proxy_attachments_rejects_estimated_total_size_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr(attachment_helpers, "MAX_PROXY_TOTAL_ATTACHMENT_BYTES", 4)

    limit = attachment_helpers.MAX_PROXY_TOTAL_ATTACHMENT_BYTES
    with pytest.raises(ValueError, match=rf"exceed {limit} bytes total"):
        await store_proxy_attachments(
            "session-1",
            [
                {"name": "one.txt", "base64": "aGVsbG8="},
            ],
        )

    assert not (tmp_path / "attachments").exists()


@pytest.mark.asyncio
async def test_store_proxy_attachments_marks_traversal_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))

    paths = await store_proxy_attachments(
        "session-1",
        [{"name": "../note.txt", "base64": "aGVsbG8="}],
    )

    assert paths[0].name.endswith("_note.txt")


@pytest.mark.asyncio
async def test_cleanup_proxy_attachments_for_session_removes_session_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    paths = await store_proxy_attachments(
        "session-1",
        [{"name": "one.txt", "base64": "MQ=="}],
    )

    removed = await cleanup_proxy_attachments_for_session("session-1")

    assert removed == 1
    assert not paths[0].exists()
    assert not paths[0].parent.exists()


@pytest.mark.asyncio
async def test_cleanup_expired_proxy_attachments_uses_retention_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    old_paths = await store_proxy_attachments(
        "old-session",
        [{"name": "old.txt", "base64": "b2xk"}],
    )
    fresh_paths = await store_proxy_attachments(
        "fresh-session",
        [{"name": "fresh.txt", "base64": "bmV3"}],
    )
    old_dir = old_paths[0].parent
    monkeypatch.setattr(attachment_helpers.time, "time", lambda: 1_000.0)
    old_mtime = 1_000.0 - attachment_helpers.PROXY_ATTACHMENT_RETENTION_SECONDS - 10
    old_dir.touch()

    os.utime(old_dir, (old_mtime, old_mtime))
    os.utime(old_paths[0], (old_mtime, old_mtime))

    removed = await cleanup_expired_proxy_attachments()

    assert removed == 1
    assert not old_dir.exists()
    assert fresh_paths[0].exists()


@pytest.mark.asyncio
async def test_cleanup_expired_proxy_attachments_keeps_directory_with_fresh_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    paths = await store_proxy_attachments(
        "mixed-session",
        [{"name": "fresh.txt", "base64": "bmV3"}],
    )
    session_dir = paths[0].parent
    monkeypatch.setattr(attachment_helpers.time, "time", lambda: 1_000.0)
    old_mtime = 1_000.0 - attachment_helpers.PROXY_ATTACHMENT_RETENTION_SECONDS - 10
    fresh_mtime = 1_000.0
    os.utime(session_dir, (old_mtime, old_mtime))
    os.utime(paths[0], (fresh_mtime, fresh_mtime))

    removed = await cleanup_expired_proxy_attachments()

    assert removed == 0
    assert paths[0].exists()
