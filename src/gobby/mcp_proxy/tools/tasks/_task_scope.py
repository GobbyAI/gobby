"""Task mandate checks shared by close and review transitions."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.tasks._context import (
    CHECKOUT_RESOLUTION_ERRORS,
    RegistryContext,
    checkout_unresolved_error,
)
from gobby.plans.semantic_lint import find_file_paths_in_text
from gobby.storage.project_checkouts import resolve_operation_root
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.tasks.acceptance_artifacts import extract_artifact_references
from gobby.utils.daemon_git import GitOk, daemon_git

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.tasks import Task


MIN_SCOPE_JUSTIFICATION_LENGTH = 20
MAX_SCOPE_JUSTIFICATION_LENGTH = 1000

_TARGET_LINE_RE = re.compile(r"^\s*Targets?\s*:\s*(?P<rest>.*)$", re.IGNORECASE)
_ACCEPTANCE_RE = re.compile(r"^\s*Acceptance\s*:", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_DECLARED_ANNOTATION_SOURCES = frozenset({"manual", "expansion"})
_ADVISORY_ANNOTATION_SOURCES = frozenset({"hypothesis"})
_TESTS_ROOT = "tests/"


@dataclass(frozen=True)
class TaskScopeEvaluation:
    """Deterministic comparison of task mandate paths with delivered paths."""

    declared_paths: tuple[str, ...]
    actual_paths: tuple[str, ...]
    out_of_scope_paths: tuple[str, ...]
    advisory_paths: tuple[str, ...] = ()
    advisory_scope_drift: tuple[str, ...] = ()
    scope_justification: str | None = None
    justification_error: str | None = None

    @property
    def has_mismatch(self) -> bool:
        return bool(self.out_of_scope_paths)

    @property
    def accepted(self) -> bool:
        return not self.has_mismatch or self.justification_error is None

    def details(self) -> dict[str, object]:
        details: dict[str, object] = {
            "declared_scope": list(self.declared_paths),
            "actual_paths": list(self.actual_paths),
            "out_of_scope_paths": list(self.out_of_scope_paths),
        }
        if self.advisory_paths:
            details["advisory_scope"] = list(self.advisory_paths)
        if self.advisory_scope_drift:
            details["advisory_scope_drift"] = list(self.advisory_scope_drift)
        return details

    def snapshot(self) -> tuple[tuple[str, ...], ...]:
        return (
            self.declared_paths,
            self.advisory_paths,
            self.actual_paths,
            self.out_of_scope_paths,
            self.advisory_scope_drift,
        )


async def evaluate_task_scope_async(
    *,
    db: HubDatabase,
    task: Task,
    commit_shas: Iterable[str],
    attributed_paths: Iterable[str],
    repo_path: str | None,
    scope_justification: str | None,
) -> TaskScopeEvaluation:
    """Compare linked and attributed paths with the task's declared mandate."""
    declared_paths, advisory_paths = _collect_task_scopes(db, task)
    actual_paths = {
        normalized
        for path in attributed_paths
        if (normalized := _normalize_repo_path(path)) is not None
    }
    commit_list = list(commit_shas)
    if (declared_paths or advisory_paths) and commit_list:
        if not repo_path:
            raise RuntimeError("No repository path is available for linked commit inspection.")
        actual_paths.update(await collect_commit_paths_async(commit_list, repo_path))

    declared_actual_paths = _paths_relevant_to_scope(actual_paths, declared_paths)
    advisory_actual_paths = _paths_relevant_to_scope(actual_paths, advisory_paths)
    out_of_scope = (
        sorted(
            path
            for path in declared_actual_paths
            if not any(_scope_entry_covers(entry, path, repo_path) for entry in declared_paths)
        )
        if declared_paths
        else []
    )
    advisory_scope_drift = (
        sorted(
            path
            for path in advisory_actual_paths
            if not any(_scope_entry_covers(entry, path, repo_path) for entry in advisory_paths)
        )
        if advisory_paths
        else []
    )
    justification, justification_error = _validate_scope_justification(
        scope_justification,
        mismatch=bool(out_of_scope),
    )
    return TaskScopeEvaluation(
        declared_paths=tuple(sorted(declared_paths)),
        actual_paths=tuple(sorted(declared_actual_paths)),
        out_of_scope_paths=tuple(out_of_scope),
        advisory_paths=tuple(sorted(advisory_paths)),
        advisory_scope_drift=tuple(advisory_scope_drift),
        scope_justification=justification,
        justification_error=justification_error,
    )


def evaluate_task_scope(**kwargs: Any) -> TaskScopeEvaluation:
    """Offline synchronous facade for direct-library consumers."""
    return asyncio.run(evaluate_task_scope_async(**kwargs))


def collect_declared_task_scope(db: HubDatabase, task: Task) -> set[str]:
    """Collect prospective scope; observed commit annotations are excluded."""
    declared, _advisory = _collect_task_scopes(db, task)
    return declared


def _collect_task_scopes(db: HubDatabase, task: Task) -> tuple[set[str], set[str]]:
    """Collect binding declarations and advisory create-time hypotheses."""
    declared: set[str] = set()
    advisory: set[str] = set()
    for annotation in TaskAffectedFileManager(db).get_files(task.id):
        normalized = _normalize_scope_entry(annotation.file_path)
        if normalized is None:
            continue
        if annotation.annotation_source in _DECLARED_ANNOTATION_SOURCES:
            declared.add(normalized)
        elif annotation.annotation_source in _ADVISORY_ANNOTATION_SOURCES:
            advisory.add(normalized)

    declared.update(collect_declared_task_targets(task.description))
    if not declared:
        return declared, advisory
    for kind in ("test", "file"):
        for reference in extract_artifact_references(task.validation_criteria or "", kind):
            normalized = _normalize_scope_entry(reference)
            if normalized is not None:
                declared.add(normalized)
    return declared, advisory


def _paths_relevant_to_scope(actual_paths: set[str], scope_paths: set[str]) -> set[str]:
    """Exclude test mirrors when a scope contains production paths."""
    if any(not _path_is_under(entry, _TESTS_ROOT) for entry in scope_paths):
        return {path for path in actual_paths if not _path_is_under(path, _TESTS_ROOT)}
    return set(actual_paths)


def collect_declared_task_targets(
    description: str | None,
    affected_files: Iterable[str] | None = None,
) -> set[str]:
    """Collect normalized paths supplied through Targets or affected_files."""
    declared: set[str] = set()
    for target_line in _iter_target_block_lines(description or ""):
        for target in find_file_paths_in_text(target_line):
            normalized = _normalize_scope_entry(target)
            if normalized is not None:
                declared.add(normalized)
    for affected_file in affected_files or ():
        normalized = _normalize_scope_entry(affected_file)
        if normalized is not None:
            declared.add(normalized)
    return declared


def find_targets_not_found(repo_path: str, targets: Iterable[str]) -> list[str]:
    """Return declared target paths that do not exist beneath the project root."""
    root = Path(repo_path)
    return sorted({target for target in targets if not (root / target).exists()})


def targets_not_found_for_request(
    ctx: RegistryContext,
    *,
    project_id: str,
    session_id: str | None,
    description: str | None,
    affected_files: list[str] | None,
) -> list[str] | dict[str, Any]:
    """Return missing targets from the session's registered operation checkout."""
    targets = collect_declared_task_targets(description, affected_files)
    if not targets:
        return []
    try:
        resolved_session = ctx.resolve_session_id(session_id) if session_id else None
        session = ctx.session_manager.get(resolved_session) if resolved_session else None
        machine_id = ctx.checkout_machine_id(project_id, session_id)
        primary = ctx.get_project_repo_path(project_id, machine_id)
        workspace = getattr(session, "workspace_path", None)
        repo_path = primary
        if workspace and (not primary or os.path.realpath(workspace) != os.path.realpath(primary)):
            repo_path = resolve_operation_root(
                ctx.task_manager.db,
                project_id,
                machine_id,
                overlay_path=workspace,
            )
    except CHECKOUT_RESOLUTION_ERRORS as exc:
        return checkout_unresolved_error(exc)
    return [] if repo_path is None else find_targets_not_found(repo_path, targets)


async def collect_commit_paths_async(commit_shas: Iterable[str], repo_path: str) -> set[str]:
    """Return normalized paths changed by each prospective linked commit."""
    paths: set[str] = set()
    for sha in commit_shas:
        result = await daemon_git.run(
            ["diff-tree", "--root", "--no-commit-id", "--name-only", "-z", "-r", sha],
            cwd=repo_path,
            timeout=10,
        )
        if not isinstance(result, GitOk):
            raise RuntimeError(f"Cannot inspect changed paths for commit {sha}.")
        for path in result.stdout.split("\0"):
            normalized = _normalize_git_repo_path(path)
            if normalized is not None:
                paths.add(normalized)
    return paths


def collect_commit_paths(commit_shas: Iterable[str], repo_path: str) -> set[str]:
    """Offline synchronous facade for direct-library consumers."""
    return asyncio.run(collect_commit_paths_async(commit_shas, repo_path))


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


def _normalize_git_repo_path(value: str) -> str | None:
    """Normalize Git's literal repo-relative output without rewriting filename bytes."""
    if not value or value.startswith("/"):
        return None
    path = PurePosixPath(value)
    if ".." in path.parts:
        return None
    return path.as_posix()


def _scope_entry_covers(entry: str, path: str, repo_path: str | None) -> bool:
    if entry.endswith("/"):
        return path.startswith(entry)
    if repo_path and Path(repo_path, entry).is_dir():
        return path == entry or path.startswith(f"{entry}/")
    return path == entry


def _path_is_under(path: str, root: str) -> bool:
    return path == root.rstrip("/") or path.startswith(root)


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
