"""This attempt's violation lines are copied by its own identity, never another run's."""

from __future__ import annotations

from pathlib import Path

import pytest
from runtime_boundary import EvidenceError
from violation_copy import copy_attempt_violations

SANDBOX_LINE = (
    '{"line": "deny(1) network-outbound", "operation": "connect", "target": "example.com:443"}\n'
)
MONITOR_LINE = '{"source": "srt-monitor", "line": "deny(2) file-write", "occurrences": 2}\n'
OTHER_RUN_LINE = '{"source": "srt-monitor", "line": "deny(9) other-run", "occurrences": 9}\n'


def _stage_attempt(root: Path, attempt: str = "rerun2611a") -> None:
    (root / "logs").mkdir(parents=True)
    (root / "logs" / f"violations-{attempt}.jsonl").write_text(SANDBOX_LINE)
    (root / "receipts").mkdir()


def test_copies_the_attempts_sandbox_violation_lines_byte_for_byte(tmp_path: Path) -> None:
    _stage_attempt(tmp_path)

    copied = copy_attempt_violations(tmp_path, "rerun2611a", monitor_dir=None)

    assert (tmp_path / "receipts/violations-rerun2611a.jsonl").read_text() == SANDBOX_LINE
    assert copied["sandbox"] == str(tmp_path / "receipts/violations-rerun2611a.jsonl")
    assert copied["monitor"] is None


def test_copies_only_the_monitor_backlog_keyed_to_this_attempt(tmp_path: Path) -> None:
    _stage_attempt(tmp_path)
    monitor = tmp_path / "monitor"
    monitor.mkdir()
    (monitor / "rerun2611a.srt-backlog.jsonl").write_text(MONITOR_LINE)
    (monitor / "other-run.srt-backlog.jsonl").write_text(OTHER_RUN_LINE)

    copied = copy_attempt_violations(tmp_path, "rerun2611a", monitor_dir=monitor)

    assert (tmp_path / "receipts/violations-rerun2611a-monitor.jsonl").read_text() == MONITOR_LINE
    assert (
        "other-run" not in (tmp_path / "receipts/violations-rerun2611a-monitor.jsonl").read_text()
    )
    assert copied["monitor"] == str(tmp_path / "receipts/violations-rerun2611a-monitor.jsonl")


def test_includes_a_backlog_that_the_monitor_renamed_to_active(tmp_path: Path) -> None:
    _stage_attempt(tmp_path)
    monitor = tmp_path / "monitor"
    monitor.mkdir()
    (monitor / "rerun2611a.srt-backlog.jsonl.active").write_text(MONITOR_LINE)

    copy_attempt_violations(tmp_path, "rerun2611a", monitor_dir=monitor)

    assert (tmp_path / "receipts/violations-rerun2611a-monitor.jsonl").read_text() == MONITOR_LINE


def test_refuses_an_attempt_whose_sandbox_violation_log_is_missing(tmp_path: Path) -> None:
    (tmp_path / "receipts").mkdir()

    with pytest.raises(EvidenceError, match="sandbox violation log missing"):
        copy_attempt_violations(tmp_path, "rerun2611a", monitor_dir=None)


def test_refuses_a_symlinked_monitor_backlog(tmp_path: Path) -> None:
    _stage_attempt(tmp_path)
    monitor = tmp_path / "monitor"
    monitor.mkdir()
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(MONITOR_LINE)
    (monitor / "rerun2611a.srt-backlog.jsonl").symlink_to(target)

    with pytest.raises(EvidenceError, match="symlink"):
        copy_attempt_violations(tmp_path, "rerun2611a", monitor_dir=monitor)


@pytest.mark.parametrize("attempt", ["", "../other", "a/b", "x" * 129])
def test_refuses_an_attempt_name_that_is_not_a_plain_identifier(
    tmp_path: Path, attempt: str
) -> None:
    _stage_attempt(tmp_path)

    with pytest.raises(EvidenceError, match="attempt"):
        copy_attempt_violations(tmp_path, attempt, monitor_dir=None)
