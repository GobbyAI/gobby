"""Remove filesystem roots owned by terminal or orphaned sandbox runs."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from gobby.agents.sandbox_policy import SRT_VIOLATIONS_RELATIVE_PATH
from gobby.agents.srt_process_cleanup import reap_srt_runner_process_tree
from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

_MINIMUM_ORPHAN_AGE_SECONDS = 60 * 60


@dataclass(frozen=True)
class SandboxReapResult:
    """Aggregate filesystem roots and bytes removed by one cleanup."""

    removed_roots: int = 0
    removed_bytes: int = 0


def _run_root_parents(gobby_home: Path) -> tuple[Path, Path]:
    return (
        gobby_home / "run" / "sandbox",
        gobby_home / "runtime" / "managed-executions",
    )


def _validate_run_id(run_id: str) -> None:
    if not run_id or run_id in {".", ".."} or Path(run_id).name != run_id:
        raise ValueError(f"invalid sandbox run id: {run_id!r}")


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _root_size(path: Path) -> int:
    """Return logical bytes without following directory symlinks."""
    root_stat = _lstat(path)
    if root_stat is None:
        return 0
    total = root_stat.st_size
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return total
    for directory, directory_names, file_names in os.walk(path, followlinks=False):
        directory_path = Path(directory)
        for name in (*directory_names, *file_names):
            child_stat = _lstat(directory_path / name)
            if child_stat is not None:
                total += child_stat.st_size
    return total


def _violation_log(root: Path) -> tuple[Path, os.stat_result] | None:
    """Return a safe regular violation log contained by a real run root."""
    root_stat = _lstat(root)
    if root_stat is None or stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return None
    logs_directory = root / SRT_VIOLATIONS_RELATIVE_PATH.parent
    logs_stat = _lstat(logs_directory)
    if logs_stat is None or stat.S_ISLNK(logs_stat.st_mode) or not stat.S_ISDIR(logs_stat.st_mode):
        return None
    source = root / SRT_VIOLATIONS_RELATIVE_PATH
    source_stat = _lstat(source)
    if source_stat is None or not stat.S_ISREG(source_stat.st_mode):
        return None
    return source, source_stat


def _retain_violation_log(source: Path, run_id: str, gobby_home: Path) -> None:
    """Atomically retain one forensic violation log before deleting its run root."""
    retention_root = gobby_home / "logs" / "sandbox-violations"
    retention_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = retention_root / f"{run_id}.jsonl"
    temporary_path: Path | None = None
    try:
        with source.open("rb") as source_file:
            with tempfile.NamedTemporaryFile(
                dir=retention_root,
                prefix=f".{run_id}.",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                shutil.copyfileobj(source_file, temporary_file)
        if temporary_path is None:
            raise RuntimeError("retained violation log temporary path was not created")
        temporary_path.chmod(0o600)
        temporary_path.replace(destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _remove_root(path: Path) -> bool:
    root_stat = _lstat(path)
    if root_stat is None:
        return False
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        path.unlink()
    else:
        shutil.rmtree(path)
    return True


def _reap_roots(run_id: str, roots: Iterable[Path], gobby_home: Path) -> SandboxReapResult:
    roots_to_remove = [root for root in roots if _lstat(root) is not None]
    violation_logs = [candidate for root in roots_to_remove if (candidate := _violation_log(root))]
    if violation_logs:
        source, _source_stat = max(violation_logs, key=lambda candidate: candidate[1].st_mtime_ns)
        _retain_violation_log(source, run_id, gobby_home)

    removed_roots = 0
    removed_bytes = 0
    for root in roots_to_remove:
        root_bytes = _root_size(root)
        if _remove_root(root):
            removed_roots += 1
            removed_bytes += root_bytes
    return SandboxReapResult(removed_roots=removed_roots, removed_bytes=removed_bytes)


def _reap_run_roots(run_id: str, gobby_home: Path) -> SandboxReapResult:
    _validate_run_id(run_id)
    return _reap_roots(
        run_id,
        (parent / run_id for parent in _run_root_parents(gobby_home)),
        gobby_home,
    )


async def reap_sandbox_run_roots(
    run_id: str,
    *,
    gobby_home: Path | None = None,
) -> SandboxReapResult:
    """Remove both filesystem roots for one known-terminal run off the event loop."""
    home = gobby_home or get_gobby_home()
    return await asyncio.to_thread(_reap_run_roots, run_id, home)


async def reap_terminal_sandbox_run(run_id: str) -> SandboxReapResult:
    """Reap a terminal run's process tree before removing its filesystem roots."""
    await reap_srt_runner_process_tree(run_id)
    return await reap_sandbox_run_roots(run_id)


def _startup_sweep(
    active_run_ids: set[str],
    gobby_home: Path,
    now: float,
) -> SandboxReapResult:
    cutoff = now - _MINIMUM_ORPHAN_AGE_SECONDS
    roots_by_run_id: dict[str, list[Path]] = {}
    for parent in _run_root_parents(gobby_home):
        parent_stat = _lstat(parent)
        if (
            parent_stat is None
            or stat.S_ISLNK(parent_stat.st_mode)
            or not stat.S_ISDIR(parent_stat.st_mode)
        ):
            continue
        for root in parent.iterdir():
            if root.name in active_run_ids:
                continue
            root_stat = _lstat(root)
            if root_stat is not None and root_stat.st_mtime < cutoff:
                roots_by_run_id.setdefault(root.name, []).append(root)

    removed_roots = 0
    removed_bytes = 0
    for run_id, roots in roots_by_run_id.items():
        result = _reap_roots(run_id, roots, gobby_home)
        removed_roots += result.removed_roots
        removed_bytes += result.removed_bytes
    return SandboxReapResult(removed_roots=removed_roots, removed_bytes=removed_bytes)


async def sweep_sandbox_run_roots(
    active_run_ids: set[str],
    *,
    gobby_home: Path | None = None,
    now: float | None = None,
) -> SandboxReapResult:
    """Remove terminal or missing run roots older than one hour."""
    home = gobby_home or get_gobby_home()
    result = await asyncio.to_thread(
        _startup_sweep,
        active_run_ids,
        home,
        time.time() if now is None else now,
    )
    if result.removed_roots:
        logger.info(
            "Reaped %d sandbox run root(s) (%d bytes)",
            result.removed_roots,
            result.removed_bytes,
        )
    return result
