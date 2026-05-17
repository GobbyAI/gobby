"""Tests for WebSocket proxy attachment helpers."""

from __future__ import annotations

import binascii
from pathlib import Path

import pytest

from gobby.servers.websocket import attachments as attachment_helpers
from gobby.servers.websocket.attachments import store_proxy_attachments

pytestmark = pytest.mark.unit


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
