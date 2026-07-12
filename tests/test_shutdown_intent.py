"""Tests for shutdown intent marker parsing."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import TextIO

import pytest

from gobby.shutdown_intent import (
    ShutdownIntent,
    _write_marker_atomically,
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


@pytest.mark.parametrize(
    "filename",
    ["shutdown_source.json", "shutdown_intent_active.json"],
)
def test_atomic_marker_write_uses_private_same_directory_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    marker = tmp_path / filename
    payload = {"source": "new", "intent": "restart", "timestamp": 2.0}
    observed_temps: list[Path] = []
    original_replace = os.replace

    def inspect_replace(source: str | Path, destination: str | Path) -> None:
        temp_path = Path(source)
        observed_temps.append(temp_path)
        assert temp_path.parent == marker.parent
        assert stat.S_IMODE(temp_path.stat().st_mode) == 0o600
        original_replace(source, destination)

    monkeypatch.setattr("gobby.shutdown_intent.os.replace", inspect_replace)

    _write_marker_atomically(marker, payload)

    assert json.loads(marker.read_text(encoding="utf-8")) == payload
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert len(observed_temps) == 1
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "filename",
    ["shutdown_source.json", "shutdown_intent_active.json"],
)
@pytest.mark.parametrize("failure_stage", ["write", "replace"])
def test_atomic_marker_failure_preserves_old_marker_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    failure_stage: str,
) -> None:
    marker = tmp_path / filename
    old_payload = {"source": "old", "intent": "stop", "timestamp": 1.0}
    marker.write_text(json.dumps(old_payload), encoding="utf-8")

    if failure_stage == "write":

        def fail_dump(_data: object, handle: TextIO) -> None:
            handle.write('{"partial"')
            raise OSError("injected write failure")

        monkeypatch.setattr("gobby.shutdown_intent.json.dump", fail_dump)
        expected_error = "injected write failure"
    else:

        def fail_replace(_source: str | Path, _destination: str | Path) -> None:
            raise OSError("injected replace failure")

        monkeypatch.setattr("gobby.shutdown_intent.os.replace", fail_replace)
        expected_error = "injected replace failure"

    with pytest.raises(OSError, match=expected_error):
        _write_marker_atomically(
            marker,
            {"source": "new", "intent": "restart", "timestamp": 2.0},
        )

    assert json.loads(marker.read_text(encoding="utf-8")) == old_payload
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "filename",
    ["shutdown_source.json", "shutdown_intent_active.json"],
)
def test_atomic_marker_concurrent_reader_sees_old_or_complete_new_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    marker = tmp_path / filename
    old_payload = {"source": "old", "intent": "stop", "timestamp": 1.0}
    new_payload = {"source": "new", "intent": "restart", "timestamp": 2.0}
    marker.write_text(json.dumps(old_payload), encoding="utf-8")
    replace_ready = threading.Event()
    allow_replace = threading.Event()
    original_replace = os.replace
    writer_errors: list[BaseException] = []

    def gated_replace(source: str | Path, destination: str | Path) -> None:
        replace_ready.set()
        if not allow_replace.wait(timeout=5):
            raise TimeoutError("reader did not release atomic replace")
        original_replace(source, destination)

    def write_marker() -> None:
        try:
            _write_marker_atomically(marker, new_payload)
        except BaseException as exc:
            writer_errors.append(exc)

    monkeypatch.setattr("gobby.shutdown_intent.os.replace", gated_replace)
    writer = threading.Thread(target=write_marker)
    writer.start()
    assert replace_ready.wait(timeout=5)

    observed = [json.loads(marker.read_text(encoding="utf-8"))]
    allow_replace.set()
    writer.join(timeout=5)
    assert not writer.is_alive()
    observed.append(json.loads(marker.read_text(encoding="utf-8")))

    assert not writer_errors
    assert observed == [old_payload, new_payload]
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_marker_uses_unique_temp_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = get_active_shutdown_marker_path(tmp_path)
    temp_names: list[str] = []
    original_replace = os.replace

    def record_replace(source: str | Path, destination: str | Path) -> None:
        temp_names.append(Path(source).name)
        original_replace(source, destination)

    monkeypatch.setattr("gobby.shutdown_intent.os.replace", record_replace)

    _write_marker_atomically(marker, {"source": "first"})
    _write_marker_atomically(marker, {"source": "second"})

    assert len(temp_names) == 2
    assert len(set(temp_names)) == 2


def test_write_shutdown_intent_preserves_existing_active_marker_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = get_shutdown_source_path(tmp_path)
    active_path = get_active_shutdown_marker_path(tmp_path)
    old_active = {"source": "old", "intent": "stop", "timestamp": 1.0}
    source_path.write_text(json.dumps(old_active), encoding="utf-8")
    active_path.write_text(json.dumps(old_active), encoding="utf-8")
    original_replace = os.replace

    def fail_active_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == active_path:
            raise OSError("active replace failed")
        original_replace(source, destination)

    monkeypatch.setattr("gobby.shutdown_intent.os.replace", fail_active_replace)

    with pytest.raises(OSError, match="active replace failed"):
        write_shutdown_intent("cli_restart", "restart", home=tmp_path)

    assert json.loads(active_path.read_text(encoding="utf-8")) == old_active
    assert json.loads(source_path.read_text(encoding="utf-8"))["source"] == "cli_restart"
    assert not list(tmp_path.glob("*.tmp"))


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
