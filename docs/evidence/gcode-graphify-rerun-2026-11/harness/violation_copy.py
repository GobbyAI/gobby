"""Copy one attempt's SRT violation lines into receipts/, selected by that attempt's identity."""

from __future__ import annotations

import re
from pathlib import Path

from runtime_boundary import EvidenceError, require, write_once

ATTEMPT_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
MONITOR_BACKLOG_SUFFIXES = ("", ".active")


def copy_attempt_violations(
    root: Path, attempt: str, *, monitor_dir: Path | None
) -> dict[str, str | None]:
    """Copy the sandbox log and this attempt's monitor backlog; another run's backlog is never read.

    The sandbox log is the attempt's own --violations file. The shared monitor keeps one
    backlog per run key, and the run key is the attempt, so that file is the identity match.
    """
    require(ATTEMPT_PATTERN.fullmatch(attempt) is not None, f"invalid attempt: {attempt!r}")
    source = root / "logs" / f"violations-{attempt}.jsonl"
    require(not source.is_symlink(), f"sandbox violation log is a symlink: {source}")
    require(source.is_file(), f"sandbox violation log missing for {attempt}")
    sandbox_copy = root / f"receipts/violations-{attempt}.jsonl"
    write_once(sandbox_copy, source.read_text(encoding="utf-8"), mode=0o600)
    monitor_copy: Path | None = None
    if monitor_dir is not None:
        backlog_text = _monitor_backlog(monitor_dir, attempt)
        if backlog_text is not None:
            monitor_copy = root / f"receipts/violations-{attempt}-monitor.jsonl"
            write_once(monitor_copy, backlog_text, mode=0o600)
    return {
        "sandbox": str(sandbox_copy),
        "monitor": None if monitor_copy is None else str(monitor_copy),
    }


def _monitor_backlog(monitor_dir: Path, attempt: str) -> str | None:
    parts: list[str] = []
    for suffix in MONITOR_BACKLOG_SUFFIXES:
        backlog = monitor_dir / f"{attempt}.srt-backlog.jsonl{suffix}"
        if backlog.is_symlink():
            raise EvidenceError(f"monitor backlog is a symlink: {backlog}")
        if backlog.is_file():
            parts.append(backlog.read_text(encoding="utf-8"))
    return "".join(parts) if parts else None
