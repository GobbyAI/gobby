"""Per-project agent workspaces: one daemon-committed Git repository under Gobby home.

Layout (addendum Decision 27, P9): ``<gobby_home>/agents_workspaces/<project_id>/`` is the
work tree, ``<gobby_home>/agents_workspaces/<project_id>.git/`` its daemon-only Git directory.
Each session writes ``<definition>/session-<seq>/``; an epic team shares ``epics/epic-<seq>/``.

The daemon is the only committer. A trigger schedules a commit and returns; a failed commit is
logged and never raised into set_handoff, compaction or session end. The nightly cron at
03:30 local time commits whatever a trigger missed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import fnmatch
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.worktree_checkpoint import CHECKPOINT_AUTHOR_EMAIL, CHECKPOINT_AUTHOR_NAME
from gobby.paths import get_gobby_home
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import LIVE_SESSION_STATUSES
from gobby.utils.daemon_git import GitOk, GitTimeout, daemon_git, parse_porcelain_v1_z

if TYPE_CHECKING:
    from gobby.agents.team_lifecycle import TeamBinding

logger = logging.getLogger(__name__)

WORKSPACE_DIR_VARIABLE = "_agent_workspace_dir"
WORKSPACE_EPIC_DIR_VARIABLE = "_agent_workspace_epic_dir"
WORKSPACE_PERSISTENT_VARIABLE = "_agent_workspace_persistent"
WORKSPACE_VARIABLE_NAMES = frozenset(
    {WORKSPACE_DIR_VARIABLE, WORKSPACE_EPIC_DIR_VARIABLE, WORKSPACE_PERSISTENT_VARIABLE}
)

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

Daemon-written. Each agent records its notes and handoff material in its own folder:
`<definition>/session-<seq>/`, or `epics/epic-<seq>/` for an epic team. The daemon commits this
tree. Agents do not run Git here, and retention deletes nothing.
"""

_PENDING: set[concurrent.futures.Future[None]] = set()
_BACKUP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="gobby-workspace-backup"
)
_SCHEDULE_LOCK = threading.Lock()
_SCHEDULED_PROJECTS: dict[str, str | None] = {}
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
)


class CronRegistration(Protocol):
    def register_handler(self, name: str, handler: Any) -> None:
        """Register a cron handler by name."""
        ...


class WorkspaceGitError(RuntimeError):
    """A workspace Git command failed or timed out."""


def workspaces_root() -> Path:
    """``<gobby_home>/agents_workspaces``, honoring ``$GOBBY_HOME``."""
    return get_gobby_home() / "agents_workspaces"


def workspace_anchor(project_id: str) -> Path:
    """The project's workspace work tree."""
    return workspaces_root() / project_id


def workspace_git_dir(project_id: str) -> Path:
    """The daemon-only Git directory for the project's workspace."""
    return workspaces_root() / f"{project_id}.git"


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
            result = subprocess.run(
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
) -> GitOk:
    anchor = workspace_anchor(project_id)
    index_lock = workspace_git_dir(project_id) / "index.lock"
    had_index_lock = index_lock.exists()
    result = await daemon_git.run(
        [
            "--literal-pathspecs",
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
    if isinstance(result, GitTimeout) and args[0] in {"add", "commit", "rm"} and not had_index_lock:
        # The timed-out Git worker has killed its process group. Only consider a
        # lock created during our write, and preserve it if any holder remains.
        _recover_index_lock(project_id, minimum_age=0)
    raise WorkspaceGitError(f"git {args[0]} failed for {project_id}: {result}")


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
        holders = subprocess.run(
            ["lsof", "-t", "--", str(lock)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if holders.returncode != 1 or holders.stdout.strip() or holders.stderr.strip():
        return False
    try:
        after = lock.lstat()
        if (before.st_dev, before.st_ino, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
        ):
            return False
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


async def _prepare_exclusions(project_id: str) -> None:
    """Exclude generated trees before Git expands status; retire old tracked copies."""
    exclude = workspace_git_dir(project_id) / "info" / "exclude"
    begin, end = "# BEGIN GOBBY WORKSPACE EXCLUDES", "# END GOBBY WORKSPACE EXCLUDES"
    text = exclude.read_text() if exclude.exists() else ""
    prefix, found, remainder = text.partition(begin)
    suffix = remainder.partition(end)[2] if found else ""
    patterns = [*sorted(_SECRET_NAMES), *(f"{name}/" for name in _GENERATED_DIRECTORIES)]
    managed = f"{prefix.rstrip()}\n{begin}\n" + "\n".join(patterns) + f"\n{end}{suffix}"
    if managed != text:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(managed)
    tracked = await _git(project_id, "ls-files", "-z")
    retired = [path for path in tracked.stdout.split("\0") if path and _excluded_path(path)]
    for chunk in _chunks(retired):
        await _git(project_id, "rm", "--cached", "--force", "--ignore-unmatch", "--", *chunk)


def _eligible_paths(anchor: Path, paths: Sequence[str]) -> list[str]:
    """Drop symlinks and regular files over the size limit; keep deletions."""
    eligible: list[str] = []
    for path in paths:
        if _excluded_path(path):
            continue
        full = anchor / path
        if full.is_symlink():
            logger.info("agent workspace skipped symlink %s", path)
            continue
        try:
            size = full.stat().st_size
        except FileNotFoundError:
            eligible.append(path)
            continue
        if size > WORKSPACE_FILE_LIMIT_BYTES:
            logger.info("agent workspace skipped oversized file %s (%d bytes)", path, size)
            continue
        eligible.append(path)
    return eligible


def _chunks(paths: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(paths), WORKSPACE_PATHSPEC_CHUNK):
        yield paths[start : start + WORKSPACE_PATHSPEC_CHUNK]


async def _commit_locked(project_id: str, trigger: str) -> str | None:
    anchor = workspace_anchor(project_id)
    await _prepare_exclusions(project_id)
    status = await _git(
        project_id, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"
    )
    if not status.stdout:
        return None
    entries = parse_porcelain_v1_z(status.stdout)
    eligible = _eligible_paths(anchor, [entry.path for entry in entries])
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
    ensure_workspace(project_id)
    if not _recover_index_lock(project_id):
        _BLOCKED_UNTIL[project_id] = time.monotonic() + _LOCK_BACKOFF_SECONDS
        return None
    try:
        return asyncio.run(_commit_locked(project_id, trigger))
    except WorkspaceGitError:
        _BLOCKED_UNTIL[project_id] = time.monotonic() + _LOCK_BACKOFF_SECONDS
        raise


async def _commit_logged(project_id: str, trigger: str) -> None:
    try:
        while True:
            try:
                await commit_workspace(project_id, trigger)
            except Exception as exc:
                logger.warning(
                    "Agent workspace commit failed (%s) for %s: %s", trigger, project_id, exc
                )
            with _SCHEDULE_LOCK:
                pending = _SCHEDULED_PROJECTS.get(project_id)
                if pending is None:
                    del _SCHEDULED_PROJECTS[project_id]
                    return
                trigger = pending
                _SCHEDULED_PROJECTS[project_id] = None
    except asyncio.CancelledError:
        with _SCHEDULE_LOCK:
            _SCHEDULED_PROJECTS.pop(project_id, None)
        raise


def schedule_workspace_commit(
    project_id: str | None,
    trigger: str,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Schedule a commit on the daemon loop and return; never raises or awaits.

    Without a running loop the trigger is dropped and the nightly cron commits the tree.
    """
    if project_id is None:
        return
    target = loop
    if target is None:
        try:
            target = asyncio.get_running_loop()
        except RuntimeError:
            target = None
    if target is None or not target.is_running():
        logger.debug("No daemon loop for workspace commit (%s); the cron will commit", trigger)
        return
    with _SCHEDULE_LOCK:
        if project_id in _SCHEDULED_PROJECTS:
            _SCHEDULED_PROJECTS[project_id] = trigger
            return
        _SCHEDULED_PROJECTS[project_id] = None
    try:
        future = asyncio.run_coroutine_threadsafe(_commit_logged(project_id, trigger), target)
    except RuntimeError as exc:
        with _SCHEDULE_LOCK:
            _SCHEDULED_PROJECTS.pop(project_id, None)
        logger.warning("Agent workspace commit not scheduled (%s): %s", trigger, exc)
        return
    with _SCHEDULE_LOCK:
        _PENDING.add(future)

    def finished(done: concurrent.futures.Future[None]) -> None:
        with _SCHEDULE_LOCK:
            _PENDING.discard(done)

    future.add_done_callback(finished)


async def drain_workspace_commits() -> None:
    """Wait for every scheduled workspace commit to finish."""
    while True:
        with _SCHEDULE_LOCK:
            pending = [asyncio.wrap_future(future) for future in _PENDING]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


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
            logger.warning("Agent workspace cron failed for %s: %s", project_id, exc)
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


def session_workspace_dir(project_id: str, definition: str | None, session_seq: int) -> Path:
    """The folder a session writes: ``<definition>/session-<seq>/`` under the anchor."""
    return workspace_anchor(project_id) / (definition or "default") / f"session-{session_seq}"


def epic_workspace_dir(project_id: str, epic_seq: int) -> Path:
    """The shared folder of an epic team: ``epics/epic-<seq>/`` under the anchor."""
    return workspace_anchor(project_id) / "epics" / f"epic-{epic_seq}"


def agent_workspace_write_dirs(variables: Mapping[str, Any]) -> list[str]:
    """Workspace folders a seat may write, from its bound workspace variables."""
    dirs: list[str] = []
    for key in (WORKSPACE_DIR_VARIABLE, WORKSPACE_EPIC_DIR_VARIABLE):
        value = variables.get(key)
        if isinstance(value, str) and value:
            dirs.append(value)
    return dirs


_SESSION_FOLDER = re.compile(r"session-(\d+)")
_CALLER_SCOPE_SQL = (
    "SELECT s.project_id, s.seq_num, v.variables ->> '_agent_type' AS definition, "
    "v.variables ->> '_agent_workspace_persistent' AS persistent "
    "FROM sessions s LEFT JOIN session_variables v ON v.session_id = s.id WHERE s.id = %s"
)
_LIVE_FOLDER_BINDER_SQL = (
    "SELECT 1 AS bound FROM sessions s JOIN session_variables v ON v.session_id = s.id "
    "WHERE s.project_id = %s AND s.status = ANY(%s) "
    "AND v.variables ->> '_agent_workspace_dir' = %s LIMIT 1"
)


def predecessor_folder_readable(
    sessions: SessionManager, caller_id: str | None, folder: str
) -> bool:
    """Whether a persistent seat may read a predecessor's folder.

    Eligible: the caller is persistent, the folder is ``<definition>/session-<seq>`` under the
    caller's anchor with a lower sequence than the caller's, and no live session of the project
    is bound to it. A live session holding the folder is the resumed predecessor or the clear
    successor that inherited the lineage, and either one keeps the folder out of reach.
    """
    caller = sessions.db.execute(_CALLER_SCOPE_SQL, (caller_id,)).fetchone()
    if (
        caller is None
        or caller["persistent"] != "true"
        or not caller["definition"]
        or caller["seq_num"] is None
    ):
        return False
    path = Path(os.path.normpath(folder))
    try:
        relative = path.relative_to(workspace_anchor(caller["project_id"]))
    except ValueError:
        return False
    if len(relative.parts) != 2 or relative.parts[0] != caller["definition"]:
        return False
    match = _SESSION_FOLDER.fullmatch(relative.parts[1])
    if match is None or int(match.group(1)) >= int(caller["seq_num"]):
        return False
    bound = sessions.db.execute(
        _LIVE_FOLDER_BINDER_SQL,
        (caller["project_id"], list(LIVE_SESSION_STATUSES), str(path)),
    ).fetchone()
    return bound is None


_SESSION_LOCATION_SQL = "SELECT project_id, seq_num FROM sessions WHERE id = %s"


def _definition_persistent(db: HubDatabase, definition: str | None, project_id: str) -> bool:
    if definition is None:
        return False
    from gobby.storage.definitions import AgentDefinitionManager

    row = AgentDefinitionManager(db).get_by_name(definition, project_id)
    return row is not None and row.definition_json.get("persistent_workspace") is True


def bind_session_workspace(
    db: HubDatabase, session_id: str, *, inherit_from: str | None = None
) -> Path | None:
    """Bind the session's folder once and return it.

    A session keeps the folder it already holds, so compaction and resume reuse it. A persistent
    definition's clear successor takes its predecessor's folder, which keeps the lineage's first
    sequence number. Any other session gets ``<definition>/session-<seq>/``.
    """
    from gobby.workflows.state_manager import SessionVariableManager

    variables = SessionVariableManager(db)
    current = variables.get_variables(session_id)
    bound = current.get(WORKSPACE_DIR_VARIABLE)
    if isinstance(bound, str) and bound:
        Path(bound).mkdir(parents=True, exist_ok=True)
        return Path(bound)
    location = db.execute(_SESSION_LOCATION_SQL, (session_id,)).fetchone()
    if location is None or location["project_id"] is None or location["seq_num"] is None:
        return None
    definition = current.get("_agent_type")
    definition = definition if isinstance(definition, str) and definition else None
    persistent = _definition_persistent(db, definition, location["project_id"])
    folder: Path | None = None
    if persistent and inherit_from is not None:
        inherited = variables.get_variable_subset(inherit_from, [WORKSPACE_DIR_VARIABLE]).get(
            WORKSPACE_DIR_VARIABLE
        )
        if isinstance(inherited, str) and inherited:
            folder = Path(inherited)
    if folder is None:
        folder = session_workspace_dir(location["project_id"], definition, location["seq_num"])
    folder.mkdir(parents=True, exist_ok=True)
    variables.merge_variables(
        session_id,
        {WORKSPACE_DIR_VARIABLE: str(folder), WORKSPACE_PERSISTENT_VARIABLE: persistent},
    )
    return folder


def workspace_handoff_references(db: HubDatabase, session_id: str | None) -> list[str]:
    """The session's bound folder as a handoff reference, so the next session can find it."""
    if session_id is None:
        return []
    from gobby.workflows.state_manager import SessionVariableManager

    bound = (
        SessionVariableManager(db)
        .get_variable_subset(session_id, [WORKSPACE_DIR_VARIABLE])
        .get(WORKSPACE_DIR_VARIABLE)
    )
    if isinstance(bound, str) and bound:
        return [f"Agent workspace: {bound}"]
    return []


_EPIC_README = """# Epic workspace

One file per topic. Append to an existing file with Edit; never overwrite another seat's notes.
"""


def _epic_folder(db: HubDatabase, binding: TeamBinding) -> Path | None:
    from gobby.storage.tasks import LocalTaskManager

    epic = LocalTaskManager(db).get_task(binding.task_id)
    if epic is None or epic.seq_num is None:
        return None
    return epic_workspace_dir(binding.project_id, epic.seq_num)


def _seat_epic_folder(db: HubDatabase, session_id: str) -> Path | None:
    """The epic folder of a seat run of a live epic team; None outside one."""
    from gobby.agents.team_lifecycle import seat_team
    from gobby.agents.team_scope import live_teams
    from gobby.storage.team_records import EPIC_MODE

    binding = seat_team(db, session_id)
    if binding is None or binding.mode != EPIC_MODE:
        return None
    live = live_teams(db, [binding.task_id]).get(binding.task_id)
    if live is None or live.team_id != binding.team_id:
        return None
    return _epic_folder(db, binding)


def spawn_workspace_write_paths(
    db: HubDatabase, session_id: str, definition: str | None
) -> list[str]:
    """The folders a spawned seat may write: its own session folder and its epic folder.

    Computed at spawn, before the session starts and binds its folder, from the same
    definition and sequence number that ``bind_session_workspace`` uses.
    """
    location = db.execute(_SESSION_LOCATION_SQL, (session_id,)).fetchone()
    if location is None or location["project_id"] is None or location["seq_num"] is None:
        return []
    folders = [session_workspace_dir(location["project_id"], definition, location["seq_num"])]
    epic = _seat_epic_folder(db, session_id)
    if epic is not None:
        folders.append(epic)
    return [str(folder) for folder in folders]


def grant_seat_workspace_paths(
    config: SandboxConfig, db: HubDatabase, session_id: str, definition: str | None
) -> SandboxConfig:
    """Return ``config`` with the seat's workspace folders among its write grants, once each.

    Spawn and resume both apply this, so a relaunched seat carries the folders its spawn
    granted even when its stored snapshot predates them.
    """
    granted = [
        path
        for path in spawn_workspace_write_paths(db, session_id, definition)
        if path not in config.extra_write_paths
    ]
    if not granted:
        return config
    return config.model_copy(update={"extra_write_paths": [*config.extra_write_paths, *granted]})


def bind_epic_workspace(db: HubDatabase, session_id: str) -> Path | None:
    """Bind the epic folder for a seat run of a live epic team; None outside one.

    Every seat run of the team binds the same folder, sub-worktree developers included. A
    single-task team has no epic folder, and a team whose epic has closed names none.
    """
    from gobby.workflows.state_manager import SessionVariableManager

    folder = _seat_epic_folder(db, session_id)
    if folder is None:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    readme = folder / "README.md"
    if not readme.exists():
        readme.write_text(_EPIC_README)
    SessionVariableManager(db).merge_variables(
        session_id, {WORKSPACE_EPIC_DIR_VARIABLE: str(folder)}
    )
    return folder


def release_epic_workspace(db: HubDatabase, binding: TeamBinding) -> Path | None:
    """Lock a closed epic team's folder by unbinding it from every holder; return the folder.

    Teardown calls this once the bound epic has closed, before it ends the seats. The returned
    folder names the final commit's trigger, ``epic-<seq>``. Single-task teams have no folder.
    """
    from gobby.storage.team_records import EPIC_MODE
    from gobby.workflows.state_manager import SessionVariableManager

    if binding.mode != EPIC_MODE:
        return None
    folder = _epic_folder(db, binding)
    if folder is not None:
        SessionVariableManager(db).withdraw_variable_value(WORKSPACE_EPIC_DIR_VARIABLE, str(folder))
    return folder


def unbind_epic_workspace(db: HubDatabase, session_id: str) -> None:
    """Clear a seat run's epic folder when the run ends; a resumed seat binds it again."""
    from gobby.workflows.state_manager import SessionVariableManager

    SessionVariableManager(db).withdraw_variable(session_id, WORKSPACE_EPIC_DIR_VARIABLE)
