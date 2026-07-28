"""Tests for sandbox metadata exposed on agent-run records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.storage.agents._sandbox_records import _MAX_COUNTED_VIOLATIONS, sandbox_record


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
