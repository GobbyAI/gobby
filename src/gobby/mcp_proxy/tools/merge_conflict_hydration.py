"""Helpers for keeping merge-resolution conflict rows aligned with Git state."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

from gobby.storage.merge_resolutions import ConflictStatus
from gobby.worktrees.merge import ConflictHunk
from gobby.worktrees.merge.conflict_parser import extract_conflict_hunks

logger = logging.getLogger(__name__)


async def collect_git_conflicts(
    worktree_path: str,
    *,
    git_manager: Any | None,
) -> list[dict[str, Any]]:
    """Read Git's current unmerged files and parse conflict markers from disk."""
    if git_manager is not None:
        result = await asyncio.to_thread(
            git_manager.run_git_command,
            ["diff", "--name-only", "--diff-filter=U"],
            cwd=worktree_path,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        conflicted_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    else:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            "--diff-filter=U",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
        conflicted_files = [
            line.strip()
            for line in stdout.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]

    conflicts: list[dict[str, Any]] = []
    for file_rel_path in conflicted_files:
        hunks = await read_conflict_hunks(worktree_path, file_rel_path)
        conflicts.append(
            {
                "file": file_rel_path,
                "hunks": hunks,
                "worktree_path": worktree_path,
            }
        )
    return conflicts


async def read_conflict_hunks(worktree_path: str, file_path: str) -> list[Any]:
    try:
        content = await asyncio.to_thread(
            (Path(worktree_path) / file_path).read_text,
            encoding="utf-8",
        )
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Failed to parse conflict markers in %s: %s", file_path, exc)
        return []
    return list(extract_conflict_hunks(content))


def store_missing_conflicts(
    merge_storage: Any,
    resolution_id: str,
    conflicts: list[dict[str, Any]],
    *,
    status: str,
) -> None:
    existing = {
        conflict.file_path for conflict in merge_storage.list_conflicts(resolution_id=resolution_id)
    }
    for conflict in conflicts:
        file_path = str(conflict.get("file") or "")
        if not file_path or file_path in existing:
            continue
        try:
            merge_storage.create_conflict(
                resolution_id=resolution_id,
                file_path=file_path,
                ours_content=_first_hunk_content(conflict, "ours"),
                theirs_content=_first_hunk_content(conflict, "theirs"),
                status=status,
            )
            existing.add(file_path)
        except sqlite3.IntegrityError:
            logger.debug("Conflict row already exists for %s in %s", file_path, resolution_id)


async def hydrate_resolution_conflicts(
    *,
    merge_storage: Any,
    worktree_manager: Any | None,
    git_manager: Any | None,
    resolution: Any,
) -> list[Any]:
    if not worktree_manager:
        return _list_conflicts(merge_storage, resolution.id)
    worktree = worktree_manager.get(resolution.worktree_id)
    if not worktree or not worktree.worktree_path:
        return _list_conflicts(merge_storage, resolution.id)
    git_conflicts = await collect_git_conflicts(worktree.worktree_path, git_manager=git_manager)
    if git_conflicts:
        store_missing_conflicts(
            merge_storage,
            resolution.id,
            git_conflicts,
            status=ConflictStatus.PENDING.value,
        )
    return _list_conflicts(merge_storage, resolution.id)


async def conflict_hunks_for_ai(conflict: Any, worktree_path: str | None) -> list[Any]:
    if worktree_path:
        hunks = await read_conflict_hunks(worktree_path, conflict.file_path)
        if hunks:
            return hunks
    return [
        ConflictHunk(
            ours=conflict.ours_content or "",
            theirs=conflict.theirs_content or "",
            base=None,
            start_line=1,
            end_line=1,
            context_before="",
            context_after="",
        )
    ]


def _first_hunk_content(conflict: dict[str, Any], attr: str) -> str | None:
    hunks = conflict.get("hunks") or []
    if not hunks:
        return None
    first = hunks[0]
    if isinstance(first, dict):
        value = first.get(attr)
    else:
        value = getattr(first, attr, None)
    return value if isinstance(value, str) else None


def _list_conflicts(merge_storage: Any, resolution_id: str) -> list[Any]:
    return list(merge_storage.list_conflicts(resolution_id=resolution_id))
