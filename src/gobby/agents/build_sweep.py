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
# Test executables one live tree keeps before its oldest unrecent copies go. Cargo relinks a
# removed test executable on its next run, so a trim costs a relink, never a wrong build.
TEST_EXECUTABLE_BUDGET_BYTES = 8 * 1024**3
_AGENT_SUFFIX = "-agent"
_PROFILES = ("debug", "release")
# Cargo's hashed executables: ``<stem>-<16 hex digits>``, the digits being the unit's metadata hash.
_HASHED_EXECUTABLE = re.compile(r"^(?P<stem>.+)-(?P<meta>[0-9a-f]{16})$")
# A unit's fingerprint directory: ``<package>-<metadata hash>``.
_FINGERPRINT_UNIT = re.compile(r"^.+-(?P<meta>[0-9a-f]{16})$")


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
    executable_budget_bytes: int = TEST_EXECUTABLE_BUDGET_BYTES,
) -> list[SweepFinding]:
    """Classify every build folder under ``root``; remove the stale unowned ones when ``apply``.

    A folder is owned when its checkout component matches a registered checkout, in any
    project and either trust domain. Folders touched within ``quiet_seconds`` are kept
    because a build may still be writing them. Links and non-directories are reported
    as refused and never followed. Owned folders also get their test executables trimmed
    to ``executable_budget_bytes``, oldest first; unowned folders are removed whole instead.
    """
    if root.is_symlink():
        return [SweepFinding(root, SweepAction.REFUSED, detail="symbolic link, not followed")]
    live = {checkout_component(checkout) for checkout in checkouts}
    clock = time.time() if now is None else now
    findings: list[SweepFinding] = []
    for folder in _build_folders(root):
        finding = _classify(root, folder, live, clock, quiet_seconds, apply=apply)
        findings.append(finding)
        if finding.action is SweepAction.KEEP_LIVE:
            findings.extend(
                _trim_test_executables(
                    root, folder, clock, quiet_seconds, executable_budget_bytes, apply=apply
                )
            )
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
    for project in _children(root, root / "cargo-target-v2"):
        if project.is_symlink() or not project.is_dir():
            folders.append(project)
            continue
        folders.extend(_children(root, project))
    folders.extend(_children(root, root / "sandbox" / "cargo-target"))
    return folders


def _path_has_link(anchor: Path, path: Path) -> bool:
    """True when a component of ``path`` below ``anchor`` is a symbolic link."""
    current = anchor
    for part in path.relative_to(anchor).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _children(anchor: Path, directory: Path) -> list[Path]:
    if _path_has_link(anchor, directory) or not directory.is_dir():
        return []
    return sorted(directory.iterdir())


def _remove_tree_nofollow(anchor: Path, folder: Path) -> None:
    """Remove ``folder`` through its parent's descriptor, opened without following links.

    ``shutil.rmtree`` with ``dir_fd`` refuses a link at ``folder`` itself and walks the
    subtree by descriptor, so a link swapped in anywhere on the way never redirects it.
    """
    parent_fd = _open_dir_nofollow(anchor, folder.parent)
    try:
        shutil.rmtree(folder.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _classify(
    anchor: Path,
    folder: Path,
    live: set[str],
    clock: float,
    quiet_seconds: float,
    *,
    apply: bool,
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
        _remove_tree_nofollow(anchor, folder)
    except OSError as exc:
        return SweepFinding(folder, SweepAction.REFUSED, size_bytes=size, detail=str(exc))
    return SweepFinding(folder, SweepAction.REMOVED, size_bytes=size)


def _test_metas(profile_fd: int) -> set[str]:
    """Metadata hashes of the profile's units whose fingerprint names a test target."""
    try:
        fingerprints_fd = os.open(".fingerprint", _DIR_FLAGS, dir_fd=profile_fd)
    except OSError:
        return set()
    metas: set[str] = set()
    try:
        units: list[tuple[str, str]] = []
        with os.scandir(fingerprints_fd) as entries:
            for entry in entries:
                match = _FINGERPRINT_UNIT.match(entry.name)
                if match is not None and entry.is_dir(follow_symlinks=False):
                    units.append((entry.name, match["meta"]))
        for name, meta in units:
            try:
                unit_fd = os.open(name, _DIR_FLAGS, dir_fd=fingerprints_fd)
            except OSError:
                continue
            try:
                if any(child.startswith("test-") for child in os.listdir(unit_fd)):
                    metas.add(meta)
            finally:
                os.close(unit_fd)
    finally:
        os.close(fingerprints_fd)
    return metas


def _trim_test_executables(
    anchor: Path,
    tree: Path,
    clock: float,
    quiet_seconds: float,
    budget_bytes: int,
    *,
    apply: bool,
) -> list[SweepFinding]:
    """Report or remove the oldest test executables above ``budget_bytes`` in a live tree.

    Only hashed executables whose unit is a test target are candidates, so bins, data
    files and unhashed names are never touched. A tree at or under the budget keeps all of
    them. Above it, the oldest copies go first and the trim stops once the tree is back
    under budget. A copy written inside the quiet window is never trimmed, so a feature or
    RUSTFLAGS variant that a build just linked survives.
    """
    findings: list[SweepFinding] = []
    for profile in _PROFILES:
        deps = tree / profile / "deps"
        # A sandboxed build owns this tree and can swap any directory in it for a link at any
        # moment, so every step from the anchor down is fd-relative and refuses links: the
        # unlink below lands in the directory that was opened, never in a re-resolved path.
        try:
            profile_fd = _open_dir_nofollow(anchor, tree / profile)
        except OSError:
            if _path_has_link(anchor, deps):
                findings.append(
                    SweepFinding(deps, SweepAction.REFUSED, detail="symbolic link, not followed")
                )
            continue
        try:
            findings.extend(
                _trim_profile(profile_fd, deps, clock, quiet_seconds, budget_bytes, apply=apply)
            )
        finally:
            os.close(profile_fd)
    return findings


def _trim_profile(
    profile_fd: int,
    deps: Path,
    clock: float,
    quiet_seconds: float,
    budget_bytes: int,
    *,
    apply: bool,
) -> list[SweepFinding]:
    try:
        deps_fd = os.open("deps", _DIR_FLAGS, dir_fd=profile_fd)
    except OSError:
        if os.path.islink(deps):
            return [SweepFinding(deps, SweepAction.REFUSED, detail="symbolic link, not followed")]
        return []
    findings: list[SweepFinding] = []
    try:
        metas = _test_metas(profile_fd)
        candidates: list[tuple[float, int, str]] = []
        with os.scandir(deps_fd) as entries:
            for entry in entries:
                match = _HASHED_EXECUTABLE.match(entry.name)
                if match is None or match["meta"] not in metas:
                    continue
                info = entry.stat(follow_symlinks=False)
                if not _is_executable_file(info):
                    continue
                candidates.append((info.st_mtime, info.st_size, entry.name))
        excess = sum(size for _, size, _ in candidates) - budget_bytes
        for mtime, size, name in sorted(candidates):
            if excess <= 0:
                break
            path = deps / name
            if clock - mtime < quiet_seconds:
                findings.append(SweepFinding(path, SweepAction.KEEP_RECENT, size_bytes=size))
                continue
            if not apply:
                findings.append(SweepFinding(path, SweepAction.WOULD_REMOVE, size_bytes=size))
                excess -= size
                continue
            try:
                os.unlink(name, dir_fd=deps_fd)
            except OSError as exc:
                findings.append(
                    SweepFinding(path, SweepAction.REFUSED, size_bytes=size, detail=str(exc))
                )
                continue
            findings.append(SweepFinding(path, SweepAction.REMOVED, size_bytes=size))
            excess -= size
    finally:
        os.close(deps_fd)
    return findings


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_dir_nofollow(anchor: Path, path: Path) -> int:
    """Open ``path`` as a directory, walking from ``anchor`` one component at a time.

    Each component is opened relative to its parent's descriptor with ``O_NOFOLLOW``, so a
    link anywhere below ``anchor`` fails the open instead of redirecting it.
    """
    fd = os.open(anchor, _DIR_FLAGS)
    try:
        for part in path.relative_to(anchor).parts:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _is_executable_file(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and bool(info.st_mode & _ANY_EXECUTE)


_ANY_EXECUTE = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


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
