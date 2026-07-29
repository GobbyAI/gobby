"""Cross-session Git commit ownership guard."""

from __future__ import annotations

import logging
import os
import posixpath
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.workflows.observer_utils import _extract_shell_command
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
}
_SHELL_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|", "&"})


@dataclass(frozen=True)
class GitCommitInvocation:
    """One Git commit invocation and its explicit pathspecs."""

    pathspecs: tuple[str, ...]

    @property
    def is_path_scoped(self) -> bool:
        return bool(self.pathspecs)


@dataclass(frozen=True)
class ForeignPathOwner:
    """Active foreign task/session attribution for one repo path."""

    path: str
    session_ref: str
    task_ref: str


def parse_git_commit_invocations(command: str) -> tuple[GitCommitInvocation, ...]:
    """Parse Git commit invocations from one shell command."""
    if not command.strip():
        return ()
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    invocations: list[GitCommitInvocation] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.rsplit("/", maxsplit=1)[-1] != "git":
            index += 1
            continue

        commit_index = _skip_git_global_options(tokens, index + 1)
        if commit_index >= len(tokens) or tokens[commit_index] != "commit":
            index += 1
            continue

        segment_end = commit_index + 1
        while segment_end < len(tokens) and tokens[segment_end] not in _SHELL_CONTROL_TOKENS:
            segment_end += 1
        segment = tokens[commit_index + 1 : segment_end]
        delimiter_index = segment.index("--") if "--" in segment else -1
        pathspecs = (
            tuple(segment[delimiter_index + 1 :])
            if delimiter_index >= 0 and delimiter_index + 1 < len(segment)
            else ()
        )
        invocations.append(GitCommitInvocation(pathspecs=pathspecs))
        index = segment_end + 1

    return tuple(invocations)


def _skip_git_global_options(tokens: list[str], index: int) -> int:
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _GIT_GLOBAL_OPTIONS_WITH_VALUE):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return index
    return index


def foreign_staged_commit_conflict(
    db: HubDatabase,
    event: HookEvent,
    *,
    session_id: str,
    project_id: str,
    project_path: str,
) -> str:
    """Return an actionable block reason when a commit would capture foreign paths."""
    invocations = parse_git_commit_invocations(_extract_shell_command(event))
    if not invocations:
        return ""

    try:
        owners = _active_foreign_path_owners(
            db,
            session_id=session_id,
            project_id=project_id,
        )
        if not owners:
            return ""

        conflicts: set[ForeignPathOwner] = set()
        staged_paths: set[str] | None = None
        for invocation in invocations:
            if invocation.is_path_scoped:
                candidate_paths = _git_paths(
                    project_path,
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--",
                    *invocation.pathspecs,
                )
            else:
                if staged_paths is None:
                    staged_paths = _git_paths(
                        project_path,
                        "diff",
                        "--cached",
                        "--name-only",
                        "-z",
                        "--diff-filter=ACDMRTUXB",
                    )
                candidate_paths = staged_paths

            for path in candidate_paths:
                conflicts.update(owners.get(path, ()))

        if not conflicts:
            return ""
        return _format_conflict_reason(conflicts)
    except Exception:
        logger.warning(
            "Cross-session commit ownership inspection failed",
            extra={"session_id": session_id, "project_id": project_id},
            exc_info=True,
        )
        return (
            "Commit blocked: Gobby could not verify staged-path ownership. "
            "Retry after the daemon and repository are available."
        )


def _active_foreign_path_owners(
    db: HubDatabase,
    *,
    session_id: str,
    project_id: str,
) -> dict[str, tuple[ForeignPathOwner, ...]]:
    rows = db.fetchall(
        """
        SELECT
            tasks.id AS task_id,
            tasks.seq_num AS task_seq_num,
            sessions.id AS session_id,
            sessions.seq_num AS session_seq_num
        FROM tasks
        JOIN sessions ON sessions.id = tasks.claimed_by_session_id
        WHERE tasks.project_id = %s
          AND tasks.claimed_by_session_id IS NOT NULL
          AND tasks.closed_at IS NULL
          AND sessions.id != %s
          AND sessions.status IN ('active', 'paused')
        ORDER BY sessions.seq_num, tasks.seq_num
        """,
        (project_id, session_id),
    )
    variable_manager = SessionVariableManager(db)
    variables_by_session: dict[str, dict[str, Any]] = {}
    owners: dict[str, list[ForeignPathOwner]] = {}

    for row in rows:
        owner_session_id = str(row["session_id"])
        variables = variables_by_session.get(owner_session_id)
        if variables is None:
            variables = variable_manager.get_variables(owner_session_id)
            variables_by_session[owner_session_id] = variables
        task_files = variables.get("task_edited_files")
        if not isinstance(task_files, dict):
            continue
        raw_paths = task_files.get(str(row["task_id"]))
        if not isinstance(raw_paths, list):
            continue

        session_ref = _format_ref(row["session_seq_num"], owner_session_id)
        task_id = str(row["task_id"])
        task_ref = _format_ref(row["task_seq_num"], task_id)
        for raw_path in raw_paths:
            path = _normalize_repo_path(raw_path)
            if path is None:
                continue
            owners.setdefault(path, []).append(
                ForeignPathOwner(
                    path=path,
                    session_ref=session_ref,
                    task_ref=task_ref,
                )
            )

    return {path: tuple(path_owners) for path, path_owners in owners.items()}


def _normalize_repo_path(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = posixpath.normpath(value.replace("\\", "/"))
    if path in {"", "."} or path.startswith("../") or path.startswith("/"):
        return None
    return path


def _git_paths(project_path: str, *args: str) -> set[str]:
    result = subprocess.run(  # nosec B603 B607 - hardcoded git command
        ["git", *args],
        cwd=Path(project_path),
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        stderr = os.fsdecode(result.stderr).strip()
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {stderr}")
    return {
        path
        for raw_path in result.stdout.split(b"\0")
        if (path := _normalize_repo_path(os.fsdecode(raw_path))) is not None
    }


def _format_ref(seq_num: object, fallback_id: str) -> str:
    return f"#{seq_num}" if isinstance(seq_num, int) else fallback_id[:8]


def _format_conflict_reason(conflicts: set[ForeignPathOwner]) -> str:
    lines = [
        "Commit blocked: staged path(s) belong to another active task/session:",
        *[
            f"- {owner.path} — session {owner.session_ref}, task {owner.task_ref}"
            for owner in sorted(
                conflicts,
                key=lambda item: (item.path, item.session_ref, item.task_ref),
            )
        ],
        "Coordinate with each owner using `gobby-agents.send_message` before retrying.",
        "Commit only your paths with `git commit --only -- <owned paths>`; "
        "foreign staged entries will remain intact.",
    ]
    return "\n".join(lines)
