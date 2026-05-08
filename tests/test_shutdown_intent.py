"""Tests for shutdown intent marker parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.shutdown_intent import (
    ShutdownIntent,
    get_active_shutdown_marker_path,
    read_shutdown_intent,
    write_shutdown_intent,
)

pytestmark = pytest.mark.unit


def test_read_shutdown_intent_consumes_malformed_marker(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown_source.json"
    marker.write_text("{not-json", encoding="utf-8")

    record = read_shutdown_intent(home=tmp_path)

    assert record.error is not None
    assert not marker.exists()
    malformed = tmp_path / "shutdown_source.json.malformed"
    assert malformed.read_text(encoding="utf-8") == "{not-json"


def test_read_shutdown_intent_preserves_malformed_marker_without_consume(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown_source.json"
    marker.write_text("{not-json", encoding="utf-8")

    record = read_shutdown_intent(home=tmp_path, consume=False)

    assert record.error is not None
    assert marker.exists()
    assert not (tmp_path / "shutdown_source.json.malformed").exists()


def test_write_shutdown_intent_records_non_consuming_active_marker(tmp_path: Path) -> None:
    write_shutdown_intent("cli_restart", "restart", home=tmp_path)

    record = read_shutdown_intent(home=tmp_path)

    assert record.intent is ShutdownIntent.RESTART
    assert not (tmp_path / "shutdown_source.json").exists()
    assert get_active_shutdown_marker_path(tmp_path).exists()


def test_read_shutdown_intent_logs_malformed_content(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = tmp_path / "shutdown_source.json"
    marker.write_text("[]", encoding="utf-8")
    caplog.set_level("WARNING", logger="gobby.shutdown_intent")

    record = read_shutdown_intent(home=tmp_path)

    assert record.error == "shutdown marker must be a JSON object"
    assert "content='[]'" in caplog.text
