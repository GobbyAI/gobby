"""Tests for sandbox metadata exposed on agent-run records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.storage.agents import AgentRun
from gobby.storage.agents._sandbox_records import _MAX_COUNTED_VIOLATIONS, sandbox_record

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
    assert record["violation_count"] == 2
    assert record["violations"] == [{"sequence": 1}, {"sequence": 2}]


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
