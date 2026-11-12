"""Nightly Git backups, exclusions, and workspace repository initialization."""

from __future__ import annotations

import asyncio
import concurrent.futures
import fnmatch
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from gobby.agents.agent_workspaces import workspace_anchor, workspace_git_dir, workspaces_root
from gobby.agents.worktree_checkpoint import CHECKPOINT_AUTHOR_EMAIL, CHECKPOINT_AUTHOR_NAME
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.daemon_git import GitOk, GitTimeout, daemon_git, parse_porcelain_v1_z
from gobby.utils.spawn import run

logger = logging.getLogger(__name__)

WORKSPACE_CRON_JOB_NAME = "gobby:agent-workspaces"

WORKSPACE_CRON_HANDLER = "agent.workspaces"

WORKSPACE_CRON_DESCRIPTION = "Commit agent workspace changes to the daemon-owned repository"

WORKSPACE_CRON_EXPRESSION = "30 3 * * *"

WORKSPACE_CRON_TRIGGER = "timer"

WORKSPACE_GIT_TIMEOUT_SECONDS = 30.0

WORKSPACE_GC_TIMEOUT_SECONDS = 120.0

WORKSPACE_GC_INTERVAL_SECONDS = 7 * 24 * 60 * 60

WORKSPACE_FILE_LIMIT_BYTES = 1 << 20

WORKSPACE_PATHSPEC_CHUNK = 200

_GC_STAMP = "gobby-gc-stamp"

_README = """# Agent workspaces

Daemon-written. Persistent definitions write `<definition>/current/`. On takeover, the
previous owner's files move to `<definition>/history/session-<seq>/`; history is read-only
recovery material. Other non-default definitions use `<definition>/session-<seq>/`, and
teams share `epics/epic-<seq>/`. Default sessions use provider-native scratch space.
Only the nightly daemon backup commits this tree. Ownership journals live in the sibling
daemon-only Git directory and are excluded from the backed-up tree. Agents do not run
Git here, and retention deletes nothing.
"""

_BACKUP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="gobby-workspace-backup"
)

_BLOCKED_UNTIL: dict[str, float] = {}

_INDEX_LOCK_MIN_AGE_SECONDS = 120.0

_LOCK_BACKOFF_SECONDS = 60.0

_SECRET_NAMES = frozenset({"bootstrap.yaml", "auth.json", ".secret_kek", "local_cli_token"})

_GENERATED_DIRECTORIES = (
    ".git",
    ".codex",
    ".claude",
    ".droid",
    ".cache",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".turbo",
    "target",
    "target-*",
    "build",
    "dist",
    "coverage",
    "gobby-pytest-home-*",
    "gobby-home",
)


class CronRegistration(Protocol):
    def register_handler(self, name: str, handler: Any) -> None:
        """Register a cron handler by name."""
        ...


class WorkspaceGitError(RuntimeError):
    """A workspace Git command failed or timed out."""


def _git_env(*, author: bool = False) -> dict[str, str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    if author:
        env.update(
            GIT_AUTHOR_NAME=CHECKPOINT_AUTHOR_NAME,
            GIT_AUTHOR_EMAIL=CHECKPOINT_AUTHOR_EMAIL,
            GIT_COMMITTER_NAME=CHECKPOINT_AUTHOR_NAME,
            GIT_COMMITTER_EMAIL=CHECKPOINT_AUTHOR_EMAIL,
        )
    return env


def _init_git_dir(project_id: str) -> None:
    """Create the daemon Git directory around the work tree; no ``.git`` lands in the tree."""
    gitdir = workspace_git_dir(project_id)
    anchor = workspace_anchor(project_id)
    for args in (
        ("init", "--quiet", "-b", "main"),
        ("config", "core.bare", "false"),
        ("config", "core.worktree", str(anchor.resolve())),
    ):
        try:
            result = run(
                ["git", f"--git-dir={gitdir}", f"--work-tree={anchor}", *args],
                capture_output=True,
                text=True,
                timeout=WORKSPACE_GIT_TIMEOUT_SECONDS,
                check=False,
                env=_git_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceGitError(f"git {args[0]} timed out") from exc
        if result.returncode != 0:
            raise WorkspaceGitError(f"git {args[0]} failed: {result.stderr.strip()}")


def ensure_workspace(project_id: str) -> Path:
    """Create the work tree, its Git directory and the daemon README if missing."""
    anchor = workspace_anchor(project_id)
    anchor.mkdir(parents=True, exist_ok=True)
    if not (workspace_git_dir(project_id) / "HEAD").is_file():
        _init_git_dir(project_id)
    readme = anchor / "README.md"
    if not readme.exists():
        readme.write_text(_README)
    return anchor


async def _git(
    project_id: str,
    *args: str,
    timeout: float = WORKSPACE_GIT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    literal_pathspecs: bool = True,
) -> GitOk:
    anchor = workspace_anchor(project_id)
    index_lock = workspace_git_dir(project_id) / "index.lock"
    had_index_lock = index_lock.exists()
    result = await daemon_git.run(
        [
            "--no-optional-locks",
            "--literal-pathspecs" if literal_pathspecs else "--no-literal-pathspecs",
            f"--git-dir={workspace_git_dir(project_id)}",
            f"--work-tree={anchor}",
            *args,
        ],
        cwd=anchor,
        timeout=timeout,
        env=dict(env) if env is not None else _git_env(),
    )
    if isinstance(result, GitOk):
        return result
    failure = f"git {args[0]} failed for {project_id}: {result}"
    if isinstance(result, GitTimeout) and args[0] in {"add", "commit", "rm"} and not had_index_lock:
        # The timed-out Git worker has killed its process group. Only consider a
        # lock created during our write, and preserve it if any holder remains.
        try:
            _recover_index_lock(project_id, minimum_age=0)
        except WorkspaceGitError as exc:
            raise WorkspaceGitError(f"{failure}; index.lock cleanup failed: {exc}") from exc
    raise WorkspaceGitError(failure)


def _recover_index_lock(
    project_id: str, *, minimum_age: float = _INDEX_LOCK_MIN_AGE_SECONDS
) -> bool:
    """Remove an old, unheld lock; uncertainty always preserves the lock."""
    lock = workspace_git_dir(project_id) / "index.lock"
    try:
        before = lock.lstat()
    except FileNotFoundError:
        return True
    if lock.is_symlink() or time.time() - before.st_mtime < minimum_age:
        return False
    try:
        # lsof walks every process's descriptors: 2 to 3.5 s on macOS, so 5 s was too tight.
        holders = run(
            ["lsof", "-w", "-t", "--", str(lock)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceGitError(f"Cannot inspect workspace index.lock with lsof: {exc}") from exc
    try:
        after = lock.lstat()
        if (before.st_dev, before.st_ino, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
        ):
            return False
    except FileNotFoundError:
        return True
    if holders.returncode not in {0, 1} or holders.stderr.strip():
        detail = holders.stderr.strip() or f"lsof exited {holders.returncode}"
        raise WorkspaceGitError(f"Cannot inspect workspace index.lock: {detail}")
    if holders.returncode != 1 or holders.stdout.strip():
        return False
    try:
        lock.unlink()
    except FileNotFoundError:
        return True
    logger.warning("Recovered unheld agent workspace index.lock for %s", project_id)
    return True


def _excluded_path(path: str) -> bool:
    parts = Path(path).parts
    return bool(parts) and (
        parts[-1] in _SECRET_NAMES
        or any(
            fnmatch.fnmatchcase(part, pattern)
            for part in parts[:-1]
            for pattern in _GENERATED_DIRECTORIES
        )
    )


def _exclude_pattern(path: str) -> str:
    """Anchored gitignore line that matches exactly one work-tree path."""
    return "/" + re.sub(r"([\\*?\[\] ])", r"\\\1", path)


def _still_skipped(full: Path) -> bool:
    """Symlinks and oversized files are skipped; a path that is gone, unreadable or small is not."""
    try:
        if full.is_symlink():
            return True
        return full.stat().st_size > WORKSPACE_FILE_LIMIT_BYTES
    except OSError:
        return False


def _sync_exclusions(project_id: str, skipped: Sequence[str] = ()) -> tuple[int, list[str]]:
    """Return newly skipped count and exact status exclusions after syncing Git ignores.

    Git status hides each recorded untracked path, so a skipped path is listed and logged
    once instead of on every backup cycle. A recorded path is dropped once it stops being skipped.
    Nonprintable names live in escaped JSON comments and use literal status pathspecs,
    since line-based ignore rules cannot represent them exactly.
    """
    anchor = workspace_anchor(project_id)
    exclude = workspace_git_dir(project_id) / "info" / "exclude"
    begin, end = "# BEGIN GOBBY WORKSPACE EXCLUDES", "# END GOBBY WORKSPACE EXCLUDES"
    text = exclude.read_text() if exclude.exists() else ""
    prefix, found, remainder = text.partition(f"{begin}\n")
    block, _, suffix = remainder.partition(f"\n{end}") if found else ("", "", "")
    previous = {
        re.sub(r"\\(.)", r"\1", line[1:]) for line in block.split("\n") if line.startswith("/")
    }
    marker = "# GOBBY SKIPPED "
    for line in block.split("\n"):
        if line.startswith(marker):
            path = json.loads(line[len(marker) :])
            if isinstance(path, str):
                previous.add(path)
    recorded = {path for path in (*previous, *skipped) if _still_skipped(anchor / path)}
    patterns = [
        *sorted(_SECRET_NAMES),
        *(f"{name}/" for name in _GENERATED_DIRECTORIES),
        *(
            _exclude_pattern(path) if path.isprintable() else marker + json.dumps(path)
            for path in sorted(recorded)
        ),
    ]
    managed = f"{prefix.rstrip()}\n{begin}\n" + "\n".join(patterns) + f"\n{end}{suffix}"
    if managed != text:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(managed)
    return len(recorded - previous), sorted(path for path in recorded if not path.isprintable())


async def _prepare_exclusions(project_id: str) -> list[str]:
    """Hide generated and still-skipped paths from Git status; retire old tracked copies."""
    _, status_exclusions = _sync_exclusions(project_id)
    tracked = await _git(project_id, "ls-files", "-z")
    retired = [path for path in tracked.stdout.split("\0") if path and _excluded_path(path)]
    for chunk in _chunks(retired):
        await _git(project_id, "rm", "--cached", "--force", "--ignore-unmatch", "--", *chunk)
    return status_exclusions


def _eligible_paths(anchor: Path, paths: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split listed paths into (eligible, skipped); deletions stay eligible."""
    eligible: list[str] = []
    skipped: list[str] = []
    for path in paths:
        if _excluded_path(path):
            continue
        if _still_skipped(anchor / path):
            skipped.append(path)
        else:
            eligible.append(path)
    return eligible, skipped


def _chunks(paths: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(paths), WORKSPACE_PATHSPEC_CHUNK):
        yield paths[start : start + WORKSPACE_PATHSPEC_CHUNK]


async def _commit_locked(project_id: str, trigger: str) -> str | None:
    anchor = workspace_anchor(project_id)
    status_exclusions = await _prepare_exclusions(project_id)
    status = await _git(
        project_id,
        "status",
        "--porcelain",
        "-z",
        "--untracked-files=all",
        "--no-renames",
        "--",
        ".",
        *(f":(exclude,literal){path}" for path in status_exclusions),
        literal_pathspecs=False,
    )
    if not status.stdout:
        return None
    entries = parse_porcelain_v1_z(status.stdout)
    eligible, skipped = _eligible_paths(anchor, [entry.path for entry in entries])
    added, _ = _sync_exclusions(project_id, skipped)
    if added:
        logger.info(
            "agent workspace skipped %d new symlinks or oversized files for %s", added, project_id
        )
    for chunk in _chunks(eligible):
        await _git(project_id, "add", "-A", "--", *chunk)
    staged = await _git(project_id, "diff", "--cached", "--name-only", "-z")
    changed = [path for path in staged.stdout.split("\0") if path]
    if not changed:
        return None
    folders = sorted({path.split("/", 1)[0] for path in changed if "/" in path})
    subject = " ".join([f"agents-ws: {trigger}", *folders])
    await _git(
        project_id,
        "commit",
        "--no-gpg-sign",
        "--quiet",
        "-m",
        subject,
        env=_git_env(author=True),
    )
    head = await _git(project_id, "rev-parse", "HEAD")
    return head.stdout.strip()


async def commit_workspace(project_id: str, trigger: str) -> str | None:
    """Commit the eligible dirty paths of a project's workspace; ``None`` when nothing changed."""
    future = _BACKUP_EXECUTOR.submit(_commit_worker, project_id, trigger)
    return await asyncio.wrap_future(future)


def _commit_worker(project_id: str, trigger: str) -> str | None:
    """One process-wide worker owns all backup I/O and parsing, across caller loops."""
    if time.monotonic() < _BLOCKED_UNTIL.get(project_id, 0):
        return None
    try:
        ensure_workspace(project_id)
        if not _recover_index_lock(project_id):
            _BLOCKED_UNTIL[project_id] = time.monotonic() + _LOCK_BACKOFF_SECONDS
            return None
        return asyncio.run(_commit_locked(project_id, trigger))
    except WorkspaceGitError:
        _BLOCKED_UNTIL[project_id] = time.monotonic() + _LOCK_BACKOFF_SECONDS
        raise


async def _maybe_gc(project_id: str) -> None:
    await asyncio.wrap_future(_BACKUP_EXECUTOR.submit(_gc_worker, project_id))


def _gc_worker(project_id: str) -> None:
    stamp = workspace_git_dir(project_id) / _GC_STAMP
    if stamp.exists() and time.time() - stamp.stat().st_mtime < WORKSPACE_GC_INTERVAL_SECONDS:
        return
    asyncio.run(_git(project_id, "gc", "--auto", "--quiet", timeout=WORKSPACE_GC_TIMEOUT_SECONDS))
    stamp.touch()


def _workspace_project_ids() -> list[str]:
    """Every project with a workspace anchor or Git directory, including unbootstrapped ones.

    A session's first write creates its anchor folder before any Git directory exists.
    """
    return sorted(
        {entry.name.removesuffix(".git") for entry in workspaces_root().glob("*") if entry.is_dir()}
    )


async def run_workspace_cron(_job: CronJob) -> str:
    """Commit every project's workspace and run its weekly ``git gc --auto``."""
    committed = 0
    failed = 0
    for project_id in await asyncio.to_thread(_workspace_project_ids):
        try:
            if await commit_workspace(project_id, WORKSPACE_CRON_TRIGGER):
                committed += 1
            await _maybe_gc(project_id)
        except Exception as exc:
            failed += 1
            await asyncio.to_thread(
                logger.warning, "Agent workspace cron failed for %s: %s", project_id, exc
            )
    return f"agent workspaces: {committed} committed, {failed} failed"


def _ensure_workspace_cron_job(cron_storage: CronJobStorage) -> None:
    action_config = {"handler": WORKSPACE_CRON_HANDLER}
    existing = cron_storage.get_job_by_name(WORKSPACE_CRON_JOB_NAME)
    if existing is None:
        cron_storage.create_job(
            project_id=PERSONAL_PROJECT_ID,
            name=WORKSPACE_CRON_JOB_NAME,
            description=WORKSPACE_CRON_DESCRIPTION,
            schedule_type="cron",
            cron_expr=WORKSPACE_CRON_EXPRESSION,
            action_type="handler",
            action_config=action_config,
            enabled=True,
            is_system=True,
        )
        return
    if not existing.is_system:
        cron_storage.mark_as_system_job(existing.id)
    was_enabled = existing.enabled
    repaired = cron_storage.reconcile_system_job_definition(
        existing.id,
        action_type="handler",
        action_config=action_config,
        description=WORKSPACE_CRON_DESCRIPTION,
        schedule_type="cron",
        cron_expr=WORKSPACE_CRON_EXPRESSION,
        interval_seconds=None,
        run_at=None,
    )
    if repaired and was_enabled and not repaired.enabled:
        cron_storage.wake_system_job(repaired.id)


def register_agent_workspace_cron(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistration,
) -> int:
    """Register the workspace commit handler and reconcile its single system row."""
    cron_executor.register_handler(WORKSPACE_CRON_HANDLER, run_workspace_cron)
    _ensure_workspace_cron_job(cron_storage)
    return 1
