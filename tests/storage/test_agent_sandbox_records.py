"""Tests for sandbox metadata exposed on agent-run records."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.agents import AgentRun, _sandbox_records
from gobby.storage.agents._sandbox_records import (
    _MAX_COUNTED_VIOLATIONS,
    _MAX_EXPOSED_COMMAND_CHARS,
    _violation_counts,
    sandbox_list_record,
    sandbox_record,
)

pytestmark = pytest.mark.unit


def test_sandbox_record_exposes_runtime_policy_and_recent_violations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_dir = gobby_home / "run" / "sandbox" / "run-1"
    run_dir.mkdir(parents=True)
    violations = run_dir / "violations.jsonl"
    violations.write_text(
        "\n".join(json.dumps({"sequence": value}) for value in range(105)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    metadata = {
        "sandbox": {
            "backend": "srt",
            "enforced": True,
            "runtime_version": "0.0.66",
            "policy_hash": "policy-hash",
            "violation_path": str(violations),
        }
    }

    full = sandbox_record(metadata, include_events=True)
    brief = sandbox_record(metadata, include_events=False)

    assert full is not None
    assert full["backend"] == "srt"
    assert full["runtime_version"] == "0.0.66"
    assert full["policy_hash"] == "policy-hash"
    assert full["violation_count"] == 105
    assert len(full["violations"]) == 100
    assert full["violations"][0] == {"sequence": 5}
    assert brief is not None
    assert brief["violation_count"] == 105
    assert "violations" not in brief


def test_sandbox_record_refuses_violation_files_outside_gobby_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"secret":"must not be exposed"}\n', encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    record = sandbox_record(
        {"sandbox": {"backend": "srt", "violation_path": str(outside)}},
        include_events=True,
    )

    assert record is not None
    assert record["violation_count"] == 0
    assert record["violations"] == []


def test_managed_execution_log_is_counted_but_other_run_assets_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "gobby-home"
    run_root = home / "runtime" / "managed-executions" / "run-1"
    log = run_root / "logs" / "violations.jsonl"
    asset = run_root / "assets" / "grant.json"
    log.parent.mkdir(parents=True)
    asset.parent.mkdir(parents=True)
    log.write_text('{"line":"deny network-outbound raw.githubusercontent.com:443"}\n')
    asset.write_text('{"secret":"must not be exposed"}\n')
    monkeypatch.setenv("GOBBY_HOME", str(home))

    metadata = {"sandbox": {"backend": "srt", "violation_path": str(log)}}
    detail = sandbox_record(metadata, include_events=False)
    listed = sandbox_list_record(metadata["sandbox"], active=True)
    assert detail is not None and detail["violation_count"] == 1
    assert listed is not None and listed["violation_count"] == 1

    metadata["sandbox"]["violation_path"] = str(asset)
    detail = sandbox_record(metadata, include_events=True)
    assert detail is not None
    assert detail["violation_count"] == 0
    assert detail["violations"] == []


def test_sandbox_record_skips_corrupt_utf8_violation_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_dir = gobby_home / "run" / "sandbox" / "run-corrupt"
    run_dir.mkdir(parents=True)
    violations = run_dir / "violations.jsonl"
    violations.write_bytes(b'{"sequence":1}\n\xff\xfe\n{"sequence":2}\n')
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    record = sandbox_record(
        {"sandbox": {"backend": "srt", "violation_path": str(violations)}},
        include_events=True,
    )

    assert record is not None
    # The count is the shared line counter's, so detail and brief agree on it.
    assert record["violation_count"] == 3
    assert record["violations"] == [{"sequence": 1}, {"sequence": 2}]


def _live_violation_log(gobby_home: Path, run_id: str, lines: int) -> Path:
    run_dir = gobby_home / "run" / "sandbox" / run_id
    run_dir.mkdir(parents=True)
    violations = run_dir / "violations.jsonl"
    violations.write_text(
        "".join(json.dumps({"sequence": value}) + "\n" for value in range(lines)),
        encoding="utf-8",
    )
    return violations


def test_sandbox_record_decodes_only_the_recent_tail_of_a_long_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    violations = _live_violation_log(gobby_home, "run-long", 5_000)
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    real_loads = json.loads
    decoded = 0

    def counting_loads(*args: Any, **kwargs: Any) -> Any:
        nonlocal decoded
        decoded += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(json, "loads", counting_loads)

    record = sandbox_record(
        {"sandbox": {"backend": "srt", "violation_path": str(violations)}},
        include_events=True,
    )

    assert record is not None
    assert record["violation_count"] == 5_000
    assert record["violations"] == [{"sequence": value} for value in range(4_900, 5_000)]
    assert decoded == 100


def test_sandbox_record_bounds_each_projected_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Existing logs carry ~59 KB commands; the detail payload keeps a bounded prefix."""
    gobby_home = tmp_path / "gobby-home"
    run_dir = gobby_home / "run" / "sandbox" / "run-wide"
    run_dir.mkdir(parents=True)
    violations = run_dir / "violations.jsonl"
    wide = "x" * 60_000
    events = [
        {"line": "deny", "command": wide, "timestamp": "2026-10-01T00:00:00Z"},
        {"line": "deny", "command": "ls", "timestamp": "2026-10-01T00:00:01Z"},
    ]
    violations.write_text("".join(json.dumps(event) + "\n" for event in events), "utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    record = sandbox_record(
        {"sandbox": {"backend": "srt", "violation_path": str(violations)}},
        include_events=True,
    )

    assert record is not None
    capped, short = record["violations"]
    assert capped["command"] == wide[:_MAX_EXPOSED_COMMAND_CHARS]
    assert capped["command_length"] == 60_000
    assert capped["command_truncated"] is True
    assert capped["line"] == "deny"
    assert short == events[1]


@pytest.mark.parametrize(
    "filler",
    [b"{" + b"x" * 126 + b"\n", b" " * 127 + b"\n"],
    ids=["malformed", "whitespace"],
)
def test_sandbox_record_tail_reads_past_unusable_lines_to_recent_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filler: bytes,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    violations = _live_violation_log(gobby_home, "run-filler", 100)
    with violations.open("ab") as handle:
        handle.write(filler * 600)
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    record = sandbox_record(
        {"sandbox": {"backend": "srt", "violation_path": str(violations)}},
        include_events=True,
    )

    assert record is not None
    assert record["violations"] == [{"sequence": value} for value in range(100)]


def test_sandbox_record_tail_window_drops_the_partial_first_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    violations = _live_violation_log(gobby_home, "run-window", 1_000)
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    monkeypatch.setattr(_sandbox_records, "_MAX_TAIL_BYTES", 100)

    record = sandbox_record(
        {"sandbox": {"backend": "srt", "violation_path": str(violations)}},
        include_events=True,
    )

    assert record is not None
    events = record["violations"]
    assert 0 < len(events) < 100
    assert events == [{"sequence": value} for value in range(1_000 - len(events), 1_000)]


def test_sandbox_brief_caps_violation_count_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_dir = gobby_home / "run" / "sandbox" / "run-large"
    run_dir.mkdir(parents=True)
    violations = run_dir / "violations.jsonl"
    violations.write_text(
        "".join(
            json.dumps({"sequence": value}) + "\n" for value in range(_MAX_COUNTED_VIOLATIONS + 1)
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    record = sandbox_record(
        {"sandbox": {"backend": "srt", "violation_path": str(violations)}},
        include_events=False,
    )

    assert record is not None
    assert record["violation_count"] == _MAX_COUNTED_VIOLATIONS
    assert record["violation_count_truncated"] is True


def test_violation_count_reads_bytes_and_matches_the_decoded_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Counting skips decoding, which halved the cold count on a 1 GB log (#23279)."""
    gobby_home = tmp_path / "gobby-home"
    log = gobby_home / "run" / "sandbox" / "run-bytes" / "violations.jsonl"
    log.parent.mkdir(parents=True)
    # CRLF, blank, whitespace-only, invalid UTF-8 and an unterminated last line.
    log.write_bytes(b'{"a":1}\r\n\n \t\r\n\xff\xfe\n{"b":2}')
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    _violation_counts.clear()
    original_open = Path.open
    modes: list[str] = []

    def record_mode(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path == log:
            modes.append(mode)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", record_mode)
    record = sandbox_list_record({"backend": "srt", "violation_path": str(log)}, active=True)

    assert record is not None
    # The text-mode counter this replaced counted the same three lines.
    assert record["violation_count"] == 3
    assert "violation_count_truncated" not in record
    assert modes == ["rb"]


def test_live_list_count_reuses_unchanged_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    log = gobby_home / "run" / "sandbox" / "run-live" / "violations.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"event":1}\n', encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    _violation_counts.clear()
    original_open = Path.open
    opens = 0

    def count_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal opens
        if path == log:
            opens += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", count_open)
    raw = {"backend": "srt", "violation_path": str(log), "violation_count": 99}
    first = sandbox_list_record(raw, active=True)
    second = sandbox_list_record(raw, active=True)
    assert first is not None and second is not None
    assert first["violation_count"] == second["violation_count"] == 1
    assert opens == 1
    with original_open(log, "a", encoding="utf-8") as handle:
        handle.write('{"event":2}\n')
    updated = sandbox_list_record(raw, active=True)
    assert updated is not None
    assert updated["violation_count"] == 2
    assert opens == 2


def test_growing_live_log_count_reads_only_appended_bytes(tmp_path: Path) -> None:
    """A poll never rescans counted bytes; ~590 MB per poll stalled the loop (#23279)."""
    log = tmp_path / "violations.jsonl"
    log.write_bytes(b'{"a":1}\n{"b":2}\n')
    _violation_counts.clear()
    assert _sandbox_records._count_violation_lines(log) == (2, False)

    # Blank the counted lines in place, then append: a rescan would count only one line.
    with log.open("r+b") as handle:
        handle.write(b" " * 7 + b"\n" + b" " * 7 + b"\n")
    with log.open("ab") as handle:
        handle.write(b'{"c":3}\n')

    assert _sandbox_records._count_violation_lines(log) == (3, False)


def test_live_log_count_waits_for_an_unterminated_line_to_end(tmp_path: Path) -> None:
    log = tmp_path / "violations.jsonl"
    log.write_bytes(b'{"a":1}\n{"b"')
    _violation_counts.clear()
    assert _sandbox_records._count_violation_lines(log) == (2, False)

    with log.open("ab") as handle:
        handle.write(b":2}\n")
    assert _sandbox_records._count_violation_lines(log) == (2, False)

    with log.open("ab") as handle:
        handle.write(b'\n{"c":3}\n')
    assert _sandbox_records._count_violation_lines(log) == (3, False)


@pytest.mark.parametrize("replacement", ["new_file", "truncated_in_place"])
def test_live_log_count_restarts_when_the_log_is_replaced(tmp_path: Path, replacement: str) -> None:
    log = tmp_path / "violations.jsonl"
    log.write_bytes(b'{"a":1}\n{"b":2}\n{"c":3}\n')
    _violation_counts.clear()
    assert _sandbox_records._count_violation_lines(log) == (3, False)

    if replacement == "new_file":
        fresh = tmp_path / "fresh.jsonl"
        fresh.write_bytes(b'{"d":4}\n{"e":5}\n{"f":6}\n{"g":7}\n')
        fresh.replace(log)
    else:
        log.write_bytes(b'{"d":4}\n')

    expected = 4 if replacement == "new_file" else 1
    assert _sandbox_records._count_violation_lines(log) == (expected, False)


def test_capped_live_log_count_reports_truncation_once_it_grows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_sandbox_records, "_MAX_COUNTED_VIOLATIONS", 2)
    log = tmp_path / "violations.jsonl"
    log.write_bytes(b'{"a":1}\n{"b":2}\n')
    _violation_counts.clear()
    assert _sandbox_records._count_violation_lines(log) == (2, False)

    with log.open("ab") as handle:
        handle.write(b'{"c":3}\n')

    assert _sandbox_records._count_violation_lines(log) == (2, True)


def test_concurrent_live_log_counts_agree(tmp_path: Path) -> None:
    log = tmp_path / "violations.jsonl"
    log.write_bytes(b"".join(b'{"n":%d}\n' % value for value in range(500)))
    _violation_counts.clear()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _sandbox_records._count_violation_lines(log), range(32)))

    assert results == [(500, False)] * 32


def test_sandbox_record_counts_retained_log_after_the_run_root_is_reaped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    reaped_log = gobby_home / "run" / "sandbox" / "run-reaped" / "logs" / "violations.jsonl"
    retention_root = gobby_home / "logs" / "sandbox-violations"
    retention_root.mkdir(parents=True)
    retained_log = retention_root / "run-reaped.jsonl"
    retained_log.write_text(
        "\n".join(json.dumps({"sequence": value}) for value in range(3)) + "\n",
        encoding="utf-8",
    )
    retained_settings = retention_root / "run-reaped.settings.json"
    retained_settings.write_text('{"policy":"resolved"}\n', encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    record = sandbox_record(
        {
            "sandbox": {
                "backend": "srt",
                "violation_path": str(reaped_log),
                "retained_violation_path": str(retained_log),
                "retained_settings_path": str(retained_settings),
            }
        },
        include_events=False,
    )

    assert record is not None
    assert record["violation_count"] == 3
    assert record["retained_violation_path"] == str(retained_log)
    assert record["retained_settings_path"] == str(retained_settings)


def test_sandbox_record_uses_frozen_count_during_retention_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    missing_live_log = home / "runtime" / "managed-executions" / "run" / "logs" / "violations.jsonl"

    record = sandbox_record(
        {
            "sandbox": {
                "backend": "srt",
                "enforced": True,
                "violation_path": str(missing_live_log),
                "violation_count": 101,
            }
        },
        include_events=False,
    )

    assert record is not None
    assert record["violation_count"] == 101
    assert "retained_violation_path" not in record


def test_sandbox_record_prefers_the_live_log_while_the_run_root_survives(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_dir = gobby_home / "run" / "sandbox" / "run-live" / "logs"
    run_dir.mkdir(parents=True)
    live_log = run_dir / "violations.jsonl"
    live_log.write_text(
        "\n".join(json.dumps({"sequence": value}) for value in range(4)) + "\n",
        encoding="utf-8",
    )
    retention_root = gobby_home / "logs" / "sandbox-violations"
    retention_root.mkdir(parents=True)
    retained_log = retention_root / "run-live.jsonl"
    retained_log.write_text('{"sequence":0}\n', encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    record = sandbox_record(
        {
            "sandbox": {
                "backend": "srt",
                "violation_path": str(live_log),
                "retained_violation_path": str(retained_log),
            }
        },
        include_events=False,
    )

    assert record is not None
    assert record["violation_count"] == 4
    assert record["retained_violation_path"] == str(retained_log)
    assert "retained_settings_path" not in record


def test_sandbox_record_refuses_retained_paths_outside_the_retention_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    gobby_home.mkdir()
    outside_log = tmp_path / "outside.jsonl"
    outside_log.write_text('{"secret":"must not be exposed"}\n', encoding="utf-8")
    outside_settings = tmp_path / "outside-settings.json"
    outside_settings.write_text('{"secret":"must not be exposed"}\n', encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    record = sandbox_record(
        {
            "sandbox": {
                "backend": "srt",
                "retained_violation_path": str(outside_log),
                "retained_settings_path": str(outside_settings),
            }
        },
        include_events=True,
    )

    assert record is not None
    assert record["violation_count"] == 0
    assert record["violations"] == []
    assert "retained_violation_path" not in record
    assert "retained_settings_path" not in record


def test_list_sandbox_record_counts_a_multiline_log_without_parsing_bodies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    retention = gobby_home / "logs" / "sandbox-violations"
    retention.mkdir(parents=True)
    violations = retention / "run-list.jsonl"
    events = [
        {"sequence": 1, "body": "first\nline"},
        {"sequence": 2, "body": "second"},
        {"sequence": 3, "body": "third"},
    ]
    violations.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    detail = sandbox_record(
        {"sandbox": {"backend": "srt", "retained_violation_path": str(violations)}},
        include_events=True,
    )
    assert detail is not None
    assert detail["violations"] == events

    def _refuse_parse(*_args: object, **_kwargs: object) -> object:
        raise json.JSONDecodeError("list must not parse bodies", "", 0)

    monkeypatch.setattr(
        "gobby.storage.agents._sandbox_records.json.loads",
        _refuse_parse,
    )
    brief = sandbox_record(
        {"sandbox": {"backend": "srt", "retained_violation_path": str(violations)}},
        include_events=False,
    )

    assert brief is not None
    assert brief["violation_count"] == 3
    assert "violations" not in brief
    assert brief["retained_violation_path"] == str(violations.resolve())


def test_list_projection_omits_violation_bodies_and_detail_keeps_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """List serialization counts a multi-line log; the detail dict still returns bodies."""
    gobby_home = tmp_path / "gobby-home"
    retention = gobby_home / "logs" / "sandbox-violations"
    retention.mkdir(parents=True)
    violations = retention / "run-list.jsonl"
    events = [
        {"sequence": 1, "body": "first\nline"},
        {"sequence": 2, "body": "second"},
        {"sequence": 3, "body": "third"},
    ]
    violations.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    created = datetime(2026, 9, 23, tzinfo=UTC)
    run = AgentRun(
        id="11111111-1111-4111-8111-111111111111",
        parent_session_id="22222222-2222-4222-8222-222222222222",
        child_session_id="33333333-3333-4333-8333-333333333333",
        provider="codex",
        prompt="list projection",
        status="success",
        created_at=created,
        updated_at=created,
        terminal_id="term-1",
        worktree_id="44444444-4444-4444-8444-444444444444",
        requested_reasoning_effort="high",
        effective_reasoning_effort="high",
        resume_metadata_json={
            "sandbox": {
                "backend": "srt",
                "retained_violation_path": str(violations),
            }
        },
    )

    detail = run.to_dict()
    assert detail["sandbox"]["violations"] == events
    assert detail["child_session_id"] == run.child_session_id

    def _refuse_parse(*_args: object, **_kwargs: object) -> object:
        raise json.JSONDecodeError("list must not parse bodies", "", 0)

    monkeypatch.setattr(
        "gobby.storage.agents._sandbox_records.json.loads",
        _refuse_parse,
    )
    listed = run.to_list_dict()

    assert listed["run_id"] == run.id
    assert listed["child_session_id"] == run.child_session_id
    assert listed["terminal_id"] == "term-1"
    assert listed["worktree_id"] == run.worktree_id
    assert listed["requested_reasoning_effort"] == "high"
    assert listed["effective_reasoning_effort"] == "high"
    sandbox = listed["sandbox"]
    assert sandbox["violation_count"] == 3
    assert "violations" not in sandbox
    assert sandbox["retained_violation_path"] == str(violations.resolve())
