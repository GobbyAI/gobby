"""Shadow records: modes, the 10,000-line cap, and best-effort writes (plan 2.1.1)."""

from __future__ import annotations

import json
import logging
import stat
import uuid
from pathlib import Path

import pytest

from gobby.ai.decisions_shadow import record

CONSUMER = "found_work"
PAYLOAD = {"state": {"description": "a red square"}, "propositions": {"found_work": "y"}}
CAP = 10_000


def _ids(path: Path) -> list[str]:
    return [json.loads(line)["id"] for line in path.read_text().splitlines()]


def test_shadow_record_permissions_cap_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # HOME and GOBBY_HOME both point under tmp_path, so the real ~/.gobby is never touched.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / ".gobby"))
    shadow = tmp_path / ".gobby" / "decisions" / "shadow"
    path = shadow / f"{CONSUMER}.jsonl"

    record(CONSUMER, PAYLOAD)
    assert stat.S_IMODE(shadow.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first = json.loads(path.read_text())
    assert uuid.UUID(first["id"]).version == 4
    assert first["consumer"] == CONSUMER
    assert len(first["content_hash"]) == 64

    # Fill to exactly the cap: no rotation at 10,000 lines (the first record is line one).
    for _ in range(CAP - 1):
        record(CONSUMER, PAYLOAD)
    before = _ids(path)
    assert len(before) == CAP

    # The 10,001st record overflows: keep the newest 5,000 lines, ending with the new record.
    record(CONSUMER, PAYLOAD)
    after = _ids(path)
    assert len(after) == CAP // 2
    assert after[:-1] == before[CAP // 2 + 1 :]
    assert after[-1] not in before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in shadow.iterdir()) == [f"{CONSUMER}.jsonl"]

    # A blocked directory: the write fails, the caller never sees an exception, and the
    # failure logs exactly one WARNING across two consecutive attempts.
    blocked_home = tmp_path / "blocked"
    (blocked_home / ".gobby").mkdir(parents=True)
    (blocked_home / ".gobby" / "decisions").write_text("not a directory")
    monkeypatch.setenv("HOME", str(blocked_home))
    monkeypatch.setenv("GOBBY_HOME", str(blocked_home / ".gobby"))
    caplog.set_level(logging.WARNING)
    record(CONSUMER, PAYLOAD)
    record(CONSUMER, PAYLOAD)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
