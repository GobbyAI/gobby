"""Task mandate checks shared by close and review transitions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from gobby.plans.semantic_lint import find_file_paths_in_text
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.utils.git import run_git_command

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.tasks import Task


MIN_SCOPE_JUSTIFICATION_LENGTH = 20
MAX_SCOPE_JUSTIFICATION_LENGTH = 1000

_TARGET_LINE_RE = re.compile(r"^\s*Targets?\s*:\s*(?P<rest>.*)$", re.IGNORECASE)
_ACCEPTANCE_RE = re.compile(r"^\s*Acceptance\s*:", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_DECLARED_ANNOTATION_SOURCES = frozenset({"manual", "expansion"})


@dataclass(frozen=True)
class TaskScopeEvaluation:
    """Deterministic comparison of task mandate paths with delivered paths."""

    declared_paths: tuple[str, ...]
    actual_paths: tuple[str, ...]
    out_of_scope_paths: tuple[str, ...]
    scope_justification: str | None = None
    justification_error: str | None = None

    @property
    def has_mismatch(self) -> bool:
        return bool(self.out_of_scope_paths)

    @property
    def accepted(self) -> bool:
        return not self.has_mismatch or self.justification_error is None

    def details(self) -> dict[str, object]:
        return {
            "declared_scope": list(self.declared_paths),
            "actual_paths": list(self.actual_paths),
            "out_of_scope_paths": list(self.out_of_scope_paths),
        }

    def snapshot(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        return self.declared_paths, self.actual_paths, self.out_of_scope_paths


def evaluate_task_scope(
    *,
    db: HubDatabase,
    task: Task,
    commit_shas: Iterable[str],
    attributed_paths: Iterable[str],
    repo_path: str | None,
    scope_justification: str | None,
) -> TaskScopeEvaluation:
    """Compare linked and attributed paths with the task's declared mandate."""
    declared_paths = collect_declared_task_scope(db, task)
    actual_paths = {
        normalized
        for path in attributed_paths
        if (normalized := _normalize_repo_path(path)) is not None
    }
    commit_list = list(commit_shas)
    if declared_paths and commit_list:
        if not repo_path:
            raise RuntimeError("No repository path is available for linked commit inspection.")
        actual_paths.update(collect_commit_paths(commit_list, repo_path))

    out_of_scope = (
        sorted(
            path
            for path in actual_paths
            if not any(_scope_entry_covers(entry, path) for entry in declared_paths)
        )
        if declared_paths
        else []
    )
    justification, justification_error = _validate_scope_justification(
        scope_justification,
        mismatch=bool(out_of_scope),
    )
    return TaskScopeEvaluation(
        declared_paths=tuple(sorted(declared_paths)),
        actual_paths=tuple(sorted(actual_paths)),
        out_of_scope_paths=tuple(out_of_scope),
        scope_justification=justification,
        justification_error=justification_error,
    )


def collect_declared_task_scope(db: HubDatabase, task: Task) -> set[str]:
    """Collect prospective scope; observed commit annotations are excluded."""
    declared: set[str] = set()
    for annotation in TaskAffectedFileManager(db).get_files(task.id):
        if annotation.annotation_source not in _DECLARED_ANNOTATION_SOURCES:
            continue
        normalized = _normalize_scope_entry(annotation.file_path)
        if normalized is not None:
            declared.add(normalized)

    for target_line in _iter_target_block_lines(task.description or ""):
        declared.update(find_file_paths_in_text(target_line))
    return declared


def collect_commit_paths(commit_shas: Iterable[str], repo_path: str) -> set[str]:
    """Return normalized paths changed by each prospective linked commit."""
    paths: set[str] = set()
    for sha in commit_shas:
        output = run_git_command(
            ["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha],
            cwd=repo_path,
            timeout=10,
        )
        if output is None:
            raise RuntimeError(f"Cannot inspect changed paths for commit {sha}.")
        for path in output.splitlines():
            normalized = _normalize_repo_path(path)
            if normalized is not None:
                paths.add(normalized)
    return paths


def _iter_target_block_lines(description: str) -> Iterable[str]:
    lines = description.splitlines()
    index = 0
    while index < len(lines):
        match = _TARGET_LINE_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        if rest := match.group("rest").strip():
            yield rest
        index += 1
        while index < len(lines):
            candidate = lines[index]
            stripped = candidate.strip()
            if not stripped:
                break
            if _TARGET_LINE_RE.match(candidate) or _ACCEPTANCE_RE.match(candidate):
                break
            if stripped.startswith("#") or stripped.startswith("`kind:"):
                break
            if _BULLET_RE.match(candidate) or "`" in candidate or "/" in candidate:
                yield candidate
                index += 1
                continue
            break


def _normalize_scope_entry(value: str) -> str | None:
    candidate = value.strip().strip("`'\"").replace("\\", "/")
    if "::" in candidate:
        candidate = candidate.split("::", 1)[0]
    keep_trailing_slash = candidate.endswith("/")
    normalized = _normalize_repo_path(candidate)
    if normalized is None:
        return None
    return f"{normalized}/" if keep_trailing_slash else normalized


def _normalize_repo_path(value: str) -> str | None:
    candidate = value.strip().replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    candidate = candidate.rstrip("/")
    if not candidate or candidate.startswith("/"):
        return None
    path = PurePosixPath(candidate)
    if ".." in path.parts:
        return None
    return path.as_posix()


def _scope_entry_covers(entry: str, path: str) -> bool:
    if entry.endswith("/"):
        return path.startswith(entry)
    return path == entry


def _validate_scope_justification(
    value: str | None,
    *,
    mismatch: bool,
) -> tuple[str | None, str | None]:
    if not mismatch:
        return None, None
    justification = (value or "").strip()
    if not justification:
        return None, "A scope_justification is required for out-of-scope paths."
    if len(justification) < MIN_SCOPE_JUSTIFICATION_LENGTH:
        return (
            None,
            f"scope_justification must be at least {MIN_SCOPE_JUSTIFICATION_LENGTH} characters.",
        )
    if len(justification) > MAX_SCOPE_JUSTIFICATION_LENGTH:
        return (
            None,
            f"scope_justification must be at most {MAX_SCOPE_JUSTIFICATION_LENGTH} characters.",
        )
    return justification, None
