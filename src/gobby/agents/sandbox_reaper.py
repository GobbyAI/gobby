"""Remove filesystem roots owned by terminal or orphaned sandbox runs."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from gobby.agents.sandbox_policy import (
    PRE_COMMIT_STORE_SPARE_NAME,
    PRE_COMMIT_STORE_SPARE_TEMP_NAME,
    SRT_VIOLATIONS_RELATIVE_PATH,
    registered_run_tmp,
)
from gobby.agents.srt_process_cleanup import reap_srt_runner_process_tree
from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

_MINIMUM_ORPHAN_AGE_SECONDS = 60 * 60


@dataclass(frozen=True)
class SandboxReapResult:
    """Aggregate filesystem roots and bytes removed by one cleanup."""

    removed_roots: int = 0
    removed_bytes: int = 0
    skipped_roots: int = 0
    first_skipped_path: Path | None = None


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


def _remove_root(path: Path, *, remove_registered_tmp: bool = True) -> bool:
    root_stat = _lstat(path)
    if root_stat is None:
        return False
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        try:
            path.unlink()
        except OSError:
            return False
        return True

    if remove_registered_tmp:
        run_tmp = registered_run_tmp(path)
        if run_tmp is not None and run_tmp.exists():
            if not _remove_root(run_tmp, remove_registered_tmp=False):
                return False

    root = Path(os.path.abspath(path))
    failed_paths: list[Path] = []

    def onexc(
        function: Callable[..., object],
        failing_path: str,
        error: BaseException,
    ) -> None:
        failed_path = Path(os.path.abspath(failing_path))
        if not isinstance(error, OSError):
            failed_paths.append(failed_path)
            return

        for candidate in (failed_path, failed_path.parent):
            if candidate != root and root not in candidate.parents:
                continue
            try:
                candidate_stat = candidate.lstat()
                if stat.S_ISLNK(candidate_stat.st_mode):
                    continue
                mode = stat.S_IMODE(candidate_stat.st_mode) | stat.S_IRUSR | stat.S_IWUSR
                if stat.S_ISDIR(candidate_stat.st_mode) or candidate_stat.st_mode & 0o111:
                    mode |= stat.S_IXUSR
                if os.chmod in os.supports_follow_symlinks:
                    os.chmod(candidate, mode, follow_symlinks=False)
                else:
                    os.chmod(candidate, mode)
            except OSError:
                continue

        try:
            if function is os.open:
                descriptor = os.open(failing_path, os.O_RDONLY | os.O_NONBLOCK)
                os.close(descriptor)
            elif function is os.scandir:
                with os.scandir(failing_path):
                    pass
            elif function is os.close:
                failed_paths.append(failed_path)
                return
            else:
                function(failing_path)
        except Exception:
            failed_paths.append(failed_path)

    shutil.rmtree(path, onexc=onexc)
    return not failed_paths and _lstat(path) is None


def _reap_roots(run_id: str, roots: Iterable[Path], gobby_home: Path) -> SandboxReapResult:
    roots_to_remove: list[Path] = []
    violation_logs: list[tuple[tuple[Path, os.stat_result], Path]] = []
    skipped_roots = 0
    first_skipped_path: Path | None = None
    for root in roots:
        try:
            if _lstat(root) is None:
                continue
            violation_log = _violation_log(root)
        except OSError:
            skipped_roots += 1
            if first_skipped_path is None:
                first_skipped_path = root
            continue
        roots_to_remove.append(root)
        if violation_log is not None:
            violation_logs.append((violation_log, root))

    retained_log_failure_root: Path | None = None
    if violation_logs:
        (source, _source_stat), source_root = max(
            violation_logs,
            key=lambda candidate: candidate[0][1].st_mtime_ns,
        )
        try:
            _retain_violation_log(source, run_id, gobby_home)
        except OSError:
            retained_log_failure_root = source_root
            skipped_roots = 1
            first_skipped_path = source_root

    removed_roots = 0
    removed_bytes = 0
    for root in roots_to_remove:
        if root == retained_log_failure_root:
            continue
        try:
            root_bytes = _root_size(root)
            root_stat = _lstat(root)
            run_tmp = (
                registered_run_tmp(root)
                if root_stat is not None and stat.S_ISDIR(root_stat.st_mode)
                else None
            )
            if run_tmp is not None:
                root_bytes += _root_size(run_tmp)
            removed = _remove_root(root)
        except OSError:
            removed = False
            root_bytes = 0
        if removed:
            removed_roots += 1
            removed_bytes += root_bytes
            continue
        try:
            root_exists = _lstat(root) is not None
        except OSError:
            root_exists = True
        if root_exists:
            skipped_roots += 1
            if first_skipped_path is None:
                first_skipped_path = root
    return SandboxReapResult(
        removed_roots=removed_roots,
        removed_bytes=removed_bytes,
        skipped_roots=skipped_roots,
        first_skipped_path=first_skipped_path,
    )


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
    skipped_roots = 0
    first_skipped_path: Path | None = None
    for parent in _run_root_parents(gobby_home):
        try:
            parent_stat = _lstat(parent)
        except OSError:
            skipped_roots += 1
            if first_skipped_path is None:
                first_skipped_path = parent
            continue
        if (
            parent_stat is None
            or stat.S_ISLNK(parent_stat.st_mode)
            or not stat.S_ISDIR(parent_stat.st_mode)
        ):
            continue
        try:
            for root in parent.iterdir():
                if root.name in {
                    PRE_COMMIT_STORE_SPARE_NAME,
                    PRE_COMMIT_STORE_SPARE_TEMP_NAME,
                }:
                    roots_by_run_id.setdefault(root.name, []).append(root)
                    continue
                if root.name in active_run_ids:
                    continue
                try:
                    root_stat = _lstat(root)
                except OSError:
                    skipped_roots += 1
                    if first_skipped_path is None:
                        first_skipped_path = root
                    continue
                if root_stat is not None and root_stat.st_mtime < cutoff:
                    roots_by_run_id.setdefault(root.name, []).append(root)
        except OSError:
            skipped_roots += 1
            if first_skipped_path is None:
                first_skipped_path = parent

    removed_roots = 0
    removed_bytes = 0
    for run_id, roots in roots_by_run_id.items():
        try:
            result = _reap_roots(run_id, roots, gobby_home)
        except OSError:
            result = SandboxReapResult(
                skipped_roots=len(roots),
                first_skipped_path=roots[0],
            )
        removed_roots += result.removed_roots
        removed_bytes += result.removed_bytes
        skipped_roots += result.skipped_roots
        if first_skipped_path is None:
            first_skipped_path = result.first_skipped_path
    return SandboxReapResult(
        removed_roots=removed_roots,
        removed_bytes=removed_bytes,
        skipped_roots=skipped_roots,
        first_skipped_path=first_skipped_path,
    )


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
    if result.removed_roots or result.skipped_roots:
        logger.info(
            "Reaped %d sandbox run root(s) (%d bytes); skipped=%d",
            result.removed_roots,
            result.removed_bytes,
            result.skipped_roots,
        )
        if result.skipped_roots:
            logger.warning(
                "Skipped sandbox run root after removal failure: %s", result.first_skipped_path
            )
    return result
