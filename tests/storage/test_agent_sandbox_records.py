"""Tests for sandbox metadata exposed on agent-run records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.storage.agents._sandbox_records import sandbox_record


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
