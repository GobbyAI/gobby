"""Remove agent Cargo build folders that no registered checkout owns."""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from gobby.agents.cargo_target import checkout_component
from gobby.agents.constants import agent_home_cache_root

QUIET_SECONDS = 24 * 60 * 60
_AGENT_SUFFIX = "-agent"
_PROFILES = ("debug", "release")
# Cargo's hashed copies of test and bin executables: ``<stem>-<16 hex digits>``.
_HASHED_EXECUTABLE = re.compile(r"^(?P<stem>.+)-[0-9a-f]{16}$")


class SweepAction(StrEnum):
    WOULD_REMOVE = "would-remove"
    REMOVED = "removed"
    KEEP_LIVE = "keep-live"
    KEEP_RECENT = "keep-recent"
    REFUSED = "refused"
    LEGACY_EMPTY = "legacy-empty"


@dataclass(frozen=True)
class SweepFinding:
    path: Path
    action: SweepAction
    size_bytes: int = 0
    detail: str = ""


def sweep_build_root(
    root: Path,
    checkouts: Iterable[Path],
    *,
    apply: bool = False,
    now: float | None = None,
    quiet_seconds: float = QUIET_SECONDS,
) -> list[SweepFinding]:
    """Classify every build folder under ``root``; remove the stale unowned ones when ``apply``.

    A folder is owned when its checkout component matches a registered checkout, in any
    project and either trust domain. Folders touched within ``quiet_seconds`` are kept
    because a build may still be writing them. Links and non-directories are reported
    as refused and never followed. Owned folders also get their superseded executables
    pruned; unowned folders are removed whole instead.
    """
    live = {checkout_component(checkout) for checkout in checkouts}
    clock = time.time() if now is None else now
    findings: list[SweepFinding] = []
    for folder in _build_folders(root):
        finding = _classify(folder, live, clock, quiet_seconds, apply=apply)
        findings.append(finding)
        if finding.action is SweepAction.KEEP_LIVE:
            findings.extend(_prune_superseded(folder, clock, quiet_seconds, apply=apply))
    return findings


def legacy_empty_cache_target() -> SweepFinding | None:
    """Report the pre-#25825 shared cargo target when it is an empty real directory.

    Never removed here: the sweep reports it so the operator removes it by hand.
    """
    legacy = agent_home_cache_root() / "cargo-target"
    if legacy.is_symlink() or not legacy.is_dir():
        return None
    if next(legacy.iterdir(), None) is not None:
        return None
    return SweepFinding(
        legacy, SweepAction.LEGACY_EMPTY, detail="empty legacy shared target; remove by hand"
    )


def _build_folders(root: Path) -> list[Path]:
    """List entries one level below each trust-domain root, without following links."""
    folders: list[Path] = []
    for project in _children(root / "cargo-target-v2"):
        if project.is_symlink() or not project.is_dir():
            folders.append(project)
            continue
        folders.extend(_children(project))
    folders.extend(_children(root / "sandbox" / "cargo-target"))
    return folders


def _children(directory: Path) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        return []
    return sorted(directory.iterdir())


def _classify(
    folder: Path, live: set[str], clock: float, quiet_seconds: float, *, apply: bool
) -> SweepFinding:
    if folder.is_symlink():
        return SweepFinding(folder, SweepAction.REFUSED, detail="symbolic link, not followed")
    if not folder.is_dir():
        return SweepFinding(folder, SweepAction.REFUSED, detail="not a directory")
    if folder.name.removesuffix(_AGENT_SUFFIX) in live:
        return SweepFinding(folder, SweepAction.KEEP_LIVE)
    try:
        newest, size = _scan(folder)
    except OSError as exc:
        return SweepFinding(folder, SweepAction.REFUSED, detail=str(exc))
    if clock - newest < quiet_seconds:
        return SweepFinding(folder, SweepAction.KEEP_RECENT, size_bytes=size)
    if not apply:
        return SweepFinding(folder, SweepAction.WOULD_REMOVE, size_bytes=size)
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        return SweepFinding(folder, SweepAction.REFUSED, size_bytes=size, detail=str(exc))
    return SweepFinding(folder, SweepAction.REMOVED, size_bytes=size)


def _prune_superseded(
    tree: Path, clock: float, quiet_seconds: float, *, apply: bool
) -> list[SweepFinding]:
    """Report or remove hashed executables that a newer build superseded in a live tree.

    The most recently written executable per name stem always stays. A superseded copy
    written inside the quiet window stays too. Only executables directly in each profile's
    ``deps`` folder are candidates, so data files and unhashed names are never touched.
    """
    findings: list[SweepFinding] = []
    for profile in _PROFILES:
        deps = tree / profile / "deps"
        if deps.is_symlink() or not deps.is_dir():
            continue
        copies: dict[str, list[tuple[float, int, Path]]] = {}
        for entry in sorted(deps.iterdir()):
            match = _HASHED_EXECUTABLE.match(entry.name)
            if match is None or not _is_executable_file(entry):
                continue
            info = entry.lstat()
            copies.setdefault(match["stem"], []).append((info.st_mtime, info.st_size, entry))
        for stem_copies in copies.values():
            stem_copies.sort()
            for mtime, size, entry in stem_copies[:-1]:
                if clock - mtime < quiet_seconds:
                    findings.append(SweepFinding(entry, SweepAction.KEEP_RECENT, size_bytes=size))
                    continue
                if not apply:
                    findings.append(SweepFinding(entry, SweepAction.WOULD_REMOVE, size_bytes=size))
                    continue
                try:
                    entry.unlink()
                except OSError as exc:
                    findings.append(
                        SweepFinding(entry, SweepAction.REFUSED, size_bytes=size, detail=str(exc))
                    )
                    continue
                findings.append(SweepFinding(entry, SweepAction.REMOVED, size_bytes=size))
    return findings


def _is_executable_file(entry: Path) -> bool:
    return not entry.is_symlink() and entry.is_file() and os.access(entry, os.X_OK)


def _propagate(error: OSError) -> None:
    raise error


def _scan(folder: Path) -> tuple[float, int]:
    """Return the newest mtime and file bytes under ``folder``.

    Links are read, never followed. An unreadable subtree raises so the caller keeps it.
    """
    newest = folder.lstat().st_mtime
    size = 0
    for dirpath, dirnames, filenames in os.walk(folder, onerror=_propagate):
        for name in (*dirnames, *filenames):
            info = os.lstat(os.path.join(dirpath, name))
            newest = max(newest, info.st_mtime)
            if not stat.S_ISDIR(info.st_mode):
                size += info.st_size
    return newest, size
