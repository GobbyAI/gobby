"""Task mandate checks shared by close and review transitions."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.tasks._context import (
    CHECKOUT_RESOLUTION_ERRORS,
    RegistryContext,
    checkout_unresolved_error,
)
from gobby.plans.semantic_lint import collect_description_target_inventory
from gobby.storage.project_checkouts import resolve_operation_root
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.tasks.acceptance_artifacts import extract_artifact_references
from gobby.tasks.commit_graph import CommitGraph
from gobby.tasks.commits import ancestry_order, collect_net_name_status_async
from gobby.utils.daemon_git import GitOk, daemon_git

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.tasks import Task


MIN_SCOPE_JUSTIFICATION_LENGTH = 20
MAX_SCOPE_JUSTIFICATION_LENGTH = 1000

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
    for target in collect_description_target_inventory(description):
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
    return await _diff_tree_paths(commit_shas, repo_path)


@dataclass(frozen=True)
class NetCommitPaths:
    """Paths a close set changes on net, those it leaves deleted, and links it never delivered."""

    changed: frozenset[str] = frozenset()
    deleted: frozenset[str] = frozenset()
    undelivered: tuple[str, ...] = ()


async def collect_net_commit_paths_async(
    commit_shas: list[str], repo_path: str, *, candidate: str | None = None
) -> NetCommitPaths:
    """Return what the linked commits change on net against the close review's base.

    A file a later link reverts, or one only edited and never committed, is absent.
    Deletion is the candidate tree's record alone: a file it tracks but the worktree
    lacks is never reported. A linked commit the candidate does not reach is not
    delivered, so it is listed as undelivered and never netted.
    """
    delivered, undelivered = await _partition_delivered_commits(commit_shas, candidate, repo_path)
    if not delivered:
        return NetCommitPaths(undelivered=tuple(undelivered))
    listing = await collect_net_name_status_async(delivered, cwd=repo_path)
    if listing is None:
        listing = await _last_touch_name_status(delivered, repo_path)
    if listing is None:
        raise RuntimeError("Cannot compute the net diff of the linked commits against their base.")
    changed: set[str] = set()
    deleted: set[str] = set()
    fields = iter(listing.split("\0"))
    for status in fields:
        kind = status.strip()[:1]
        if not kind:
            continue
        # Renames and copies name a source then a destination; a rename removes its source.
        names = [next(fields, ""), next(fields, "")] if kind in "RC" else [next(fields, "")]
        paths = [_normalize_git_repo_path(name) for name in names]
        if kind == "C":
            paths = paths[1:]
        for path in paths:
            if path is not None:
                changed.add(path)
        if kind in "DR" and paths[0] is not None:
            deleted.add(paths[0])
    if candidate is not None and changed:
        tracked = await daemon_git.run(
            ["ls-tree", "-r", "--name-only", "-z", candidate], cwd=repo_path, timeout=10
        )
        if not isinstance(tracked, GitOk) or tracked.stderr:
            raise RuntimeError("Cannot determine changed paths absent from the close candidate.")
        # A foreign commit may delete or re-add a path after the last linked touch.
        # Validation targets must exist in the delivered candidate, not the replay.
        deleted = changed.difference(tracked.stdout.split("\0"))
    return NetCommitPaths(frozenset(changed), frozenset(deleted), tuple(undelivered))


async def _partition_delivered_commits(
    commit_shas: list[str], candidate: str | None, repo_path: str
) -> tuple[list[str], list[str]]:
    """Split the linked commits into those the close candidate reaches and those it does not.

    A rebase leaves the pre-replay originals linked beside their replays, since both
    carry the task tag, and the candidate delivers only the commits it reaches.
    """
    if candidate is None:
        return list(commit_shas), []
    if not commit_shas:
        return [], []
    graph = await CommitGraph.load([*commit_shas, candidate], cwd=repo_path, timeout=10)
    if graph is None:
        raise RuntimeError(
            "Cannot check whether the close candidate delivers commits " + ", ".join(commit_shas)
        )
    delivered: list[str] = []
    undelivered: list[str] = []
    for sha in commit_shas:
        if graph.is_ancestor(sha, candidate):
            delivered.append(sha)
        else:
            undelivered.append(sha)
    return delivered, undelivered


async def _last_touch_name_status(commit_shas: list[str], repo_path: str) -> str | None:
    """List each path once, with the status of the last linked commit that touches it.

    Replay can fail when foreign commits are interleaved between links. Keep
    every path a link touches, even if another link reverts it. A merge's
    first-parent diff can carry unlinked content (#23314), so only its remerge
    diff contributes paths. Unsupported merges and unavailable evidence fail closed.
    """
    ordered = await ancestry_order(commit_shas, cwd=repo_path)
    if ordered is None:
        return None
    parents = await daemon_git.run(
        ["rev-list", "--no-walk", "--parents", *ordered], cwd=repo_path, timeout=10
    )
    if not isinstance(parents, GitOk) or parents.stderr:
        return None
    parent_counts: dict[str, int] = {}
    for line in parents.stdout.splitlines():
        fields = line.split()
        if not fields or fields[0] not in ordered or fields[0] in parent_counts or len(fields) > 3:
            return None
        parent_counts[fields[0]] = len(fields) - 1
    if set(parent_counts) != set(ordered):
        return None
    last: dict[str, str] = {}
    for sha in ordered:
        command = (
            ["show", "--remerge-diff", "--format="]
            if parent_counts[sha] == 2
            else ["diff-tree", "--root", "--no-commit-id", "-r"]
        )
        result = await daemon_git.run(
            [*command, "--name-status", "--no-renames", "-z", sha],
            cwd=repo_path,
            timeout=10,
        )
        if not isinstance(result, GitOk) or result.stderr:
            return None
        if not result.stdout:
            continue
        fields = result.stdout.split("\0")
        if fields.pop() != "" or len(fields) % 2:
            return None
        for status, path in zip(fields[::2], fields[1::2], strict=True):
            if status not in {"A", "D", "M", "T"} or _normalize_git_repo_path(path) is None:
                return None
            last[path] = status
    return "".join(f"{status}\0{path}\0" for path, status in last.items())


async def _diff_tree_paths(commit_shas: Iterable[str], repo_path: str) -> set[str]:
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
