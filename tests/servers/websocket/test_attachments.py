"""Tests for WebSocket proxy attachment helpers."""

from __future__ import annotations

import binascii
from pathlib import Path

import pytest

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
