"""Tests for shutdown intent marker parsing."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from gobby.shutdown_intent import (
    ShutdownIntent,
    get_active_shutdown_marker_path,
    get_shutdown_source_path,
    read_active_shutdown_intent,
    read_shutdown_intent,
    read_shutdown_source_record,
    recover_stale_restart_intent,
    write_shutdown_intent,
)

pytestmark = pytest.mark.unit


def test_read_shutdown_intent_missing_marker_defaults_to_stop(tmp_path: Path) -> None:
    record = read_shutdown_intent(home=tmp_path)

    assert record.intent is ShutdownIntent.STOP
    assert record.source == "external_sigterm"
    assert record.sender_pid is None
    assert record.timestamp is None
    assert record.stale is False


def test_read_shutdown_intent_consumes_stale_marker(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown_intent_active.json"
    payload = {
        "source": "cli_restart",
        "intent": "restart",
        "sender_pid": 123,
        "timestamp": time.time() - 60,
    }
    marker.write_text(json.dumps(payload), encoding="utf-8")

    record = read_shutdown_intent(home=tmp_path, max_age_seconds=10)

    assert record.intent is ShutdownIntent.STOP
    assert record.source == "cli_restart"
    assert record.sender_pid == 123
    assert record.stale is True
    assert record.raw == payload
    assert not marker.exists()


@pytest.mark.parametrize(
    ("age_seconds", "expected_intent", "expected_stale"),
    [
        pytest.param(30.0, ShutdownIntent.RESTART, False, id="thirty-seconds"),
        pytest.param(119.999, ShutdownIntent.RESTART, False, id="inside-boundary"),
        pytest.param(120.0, ShutdownIntent.STOP, True, id="at-boundary"),
    ],
)
def test_recover_consumed_restart_marker_with_extended_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    age_seconds: float,
    expected_intent: ShutdownIntent,
    expected_stale: bool,
) -> None:
    now = 1_000.0
    monkeypatch.setattr("gobby.shutdown_intent.time.time", lambda: now)
    marker = tmp_path / "shutdown_intent_active.json"
    marker.write_text(
        json.dumps(
            {
                "source": "cli_restart",
                "intent": "restart",
                "sender_pid": 123,
                "timestamp": now - age_seconds,
            }
        ),
        encoding="utf-8",
    )

    consumed = read_shutdown_intent(home=tmp_path)
    recovered = recover_stale_restart_intent(consumed, max_age_seconds=120.0)

    assert consumed.stale is True
    assert not marker.exists()
    assert recovered.intent is expected_intent
    assert recovered.stale is expected_stale


@pytest.mark.parametrize("raw_intent", ["stop", "invalid"])
def test_recover_consumed_marker_preserves_non_restart_safety(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_intent: str,
) -> None:
    now = 1_000.0
    monkeypatch.setattr("gobby.shutdown_intent.time.time", lambda: now)
    marker = tmp_path / "shutdown_intent_active.json"
    marker.write_text(
        json.dumps(
            {
                "source": "cli_stop",
                "intent": raw_intent,
                "sender_pid": 123,
                "timestamp": now - 30.0,
            }
        ),
        encoding="utf-8",
    )

    consumed = read_shutdown_intent(home=tmp_path)
    recovered = recover_stale_restart_intent(consumed, max_age_seconds=120.0)

    assert recovered is consumed
    assert recovered.intent is ShutdownIntent.STOP
    assert recovered.stale is True


def test_read_shutdown_intent_preserves_stale_marker_without_consume(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown_intent_active.json"
    marker.write_text(
        json.dumps(
            {
                "source": "cli_restart",
                "intent": "restart",
                "sender_pid": 123,
                "timestamp": time.time() - 60,
            }
        ),
        encoding="utf-8",
    )

    record = read_shutdown_intent(home=tmp_path, consume=False, max_age_seconds=10)

    assert record.intent is ShutdownIntent.STOP
    assert record.stale is True
    assert marker.exists()


def test_restart_intent_round_trip_consume_false_then_default(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown_intent_active.json"
    write_shutdown_intent("cli_restart", ShutdownIntent.RESTART, sender_pid=456, home=tmp_path)

    preview = read_shutdown_intent(home=tmp_path, consume=False)

    assert preview.intent is ShutdownIntent.RESTART
    assert preview.source == "cli_restart"
    assert preview.sender_pid == 456
    assert marker.exists()

    consumed = read_shutdown_intent(home=tmp_path)

    assert consumed.intent is ShutdownIntent.RESTART
    assert consumed.source == "cli_restart"
    assert consumed.sender_pid == 456
    assert not marker.exists()


def test_read_shutdown_intent_consumes_malformed_marker(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown_intent_active.json"
    marker.write_text("{not-json", encoding="utf-8")

    record = read_shutdown_intent(home=tmp_path)

    assert record.error is not None
    assert not marker.exists()
    malformed = tmp_path / "shutdown_intent_active.json.malformed"
    assert malformed.read_text(encoding="utf-8") == "{not-json"


def test_read_shutdown_intent_preserves_malformed_marker_without_consume(tmp_path: Path) -> None:
    marker = tmp_path / "shutdown_intent_active.json"
    marker.write_text("{not-json", encoding="utf-8")

    record = read_shutdown_intent(home=tmp_path, consume=False)

    assert record.error is not None
    assert marker.exists()
    assert not (tmp_path / "shutdown_intent_active.json.malformed").exists()


def test_write_shutdown_intent_records_active_marker(tmp_path: Path) -> None:
    write_shutdown_intent("cli_restart", "restart", home=tmp_path)

    record = read_shutdown_intent(home=tmp_path)
    source_record = read_shutdown_source_record(home=tmp_path)

    assert record.intent is ShutdownIntent.RESTART
    assert not (tmp_path / "shutdown_intent_active.json").exists()
    assert source_record is not None
    assert source_record.source == "cli_restart"
    assert get_shutdown_source_path(tmp_path).exists()


def test_write_shutdown_intent_cleans_partial_markers_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_path = get_shutdown_source_path(tmp_path)
    active_path = get_active_shutdown_marker_path(tmp_path)
    original_write_text = Path.write_text

    def write_text_with_active_failure(
        self: Path,
        data: str,
        *args: object,
        **kwargs: object,
    ) -> int:
        if self == active_path:
            raise OSError("active write failed")
        return original_write_text(self, data, *args, **kwargs)

    caplog.set_level("ERROR", logger="gobby.shutdown_intent")
    monkeypatch.setattr(Path, "write_text", write_text_with_active_failure)

    with pytest.raises(OSError, match="active write failed"):
        write_shutdown_intent("cli_restart", "restart", home=tmp_path)

    assert not source_path.exists()
    assert not active_path.exists()
    assert "Failed to write shutdown intent markers" in caplog.text


def test_read_active_shutdown_intent_returns_none_after_consuming_marker(
    tmp_path: Path,
) -> None:
    write_shutdown_intent("cli_restart", ShutdownIntent.RESTART, sender_pid=789, home=tmp_path)
    consumed = read_shutdown_intent(home=tmp_path)

    active = read_active_shutdown_intent(home=tmp_path)
    source_record = read_shutdown_source_record(home=tmp_path)

    assert consumed.intent is ShutdownIntent.RESTART
    assert active is None
    assert source_record is not None
    assert source_record.intent is ShutdownIntent.RESTART
    assert source_record.sender_pid == 789


def test_read_active_shutdown_intent_returns_stale_record(tmp_path: Path) -> None:
    marker = get_active_shutdown_marker_path(tmp_path)
    marker.write_text(
        json.dumps(
            {
                "source": "cli_restart",
                "intent": "restart",
                "sender_pid": 123,
                "timestamp": time.time() - 60,
            }
        ),
        encoding="utf-8",
    )

    active = read_active_shutdown_intent(home=tmp_path, max_age_seconds=10)

    assert active is not None
    assert active.intent is ShutdownIntent.STOP
    assert active.stale is True


def test_read_shutdown_intent_logs_malformed_content(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    enable_log_propagation: None,
) -> None:
    marker = tmp_path / "shutdown_intent_active.json"
    marker.write_text("[]", encoding="utf-8")
    caplog.set_level("WARNING", logger="gobby.shutdown_intent")

    record = read_shutdown_intent(home=tmp_path)

    assert record.error == "shutdown marker must be a JSON object"
    assert "content='[]'" in caplog.text
