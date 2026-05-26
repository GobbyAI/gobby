"""Tests for runner chat attachment cleanup helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.runner_maintenance import _remove_stale_chat_attachment_file

pytestmark = pytest.mark.unit


def test_remove_stale_chat_attachment_file_requires_managed_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    outside = tmp_path / "outside.txt"
    outside.write_text("stale")

    assert _remove_stale_chat_attachment_file(str(outside)) is False
    assert outside.exists()


def test_remove_stale_chat_attachment_file_ignores_missing_managed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    missing = gobby_home / "projects" / "_personal" / "attachments" / "missing.txt"

    assert _remove_stale_chat_attachment_file(str(missing)) is True
