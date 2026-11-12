"""Per-project agent workspaces: one daemon-committed Git repository under Gobby home.

Layout (addendum Decision 27, P9): ``<gobby_home>/agents_workspaces/<project_id>/`` is the
work tree, ``<gobby_home>/agents_workspaces/<project_id>.git/`` its daemon-only Git directory.
Persistent definitions write ``<definition>/current/`` with prior owners archived under
``<definition>/history/session-<seq>/``. Other non-default definitions write
``<definition>/session-<seq>/``; an epic team shares ``epics/epic-<seq>/``.
The default definition uses provider-native scratch space and has no agent workspace.

The daemon is the only committer. The nightly cron at 03:30 local time is the only backup
trigger; lifecycle events (handoff, compaction, session start or end, team teardown) never
commit the workspace.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.agents.sandbox import SandboxConfig
from gobby.paths import get_gobby_home
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import LIVE_SESSION_STATUSES
from gobby.workflows.agent_models import holds_capability

if TYPE_CHECKING:
    from gobby.agents.team_admission_records import TeamBinding

logger = logging.getLogger(__name__)

WORKSPACE_DIR_VARIABLE = "_agent_workspace_dir"
WORKSPACE_EPIC_DIR_VARIABLE = "_agent_workspace_epic_dir"
WORKSPACE_PERSISTENT_VARIABLE = "_agent_workspace_persistent"
WORKSPACE_DEFINITION_VARIABLE = "_agent_workspace_definition"
WORKSPACE_VARIABLE_NAMES = frozenset(
    {
        WORKSPACE_DIR_VARIABLE,
        WORKSPACE_EPIC_DIR_VARIABLE,
        WORKSPACE_PERSISTENT_VARIABLE,
        WORKSPACE_DEFINITION_VARIABLE,
    }
)


def workspaces_root() -> Path:
    """``<gobby_home>/agents_workspaces``, honoring ``$GOBBY_HOME``."""
    return get_gobby_home() / "agents_workspaces"


def workspace_anchor(project_id: str) -> Path:
    """The project's workspace work tree."""
    return workspaces_root() / project_id


def workspace_git_dir(project_id: str) -> Path:
    """The daemon-only Git directory for the project's workspace."""
    return workspaces_root() / f"{project_id}.git"


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
    return _predecessor_folder_readable(
        sessions.db, caller["project_id"], caller["definition"], int(caller["seq_num"]), folder
    )


def _predecessor_folder_readable(
    db: HubDatabase, project_id: str, definition: str, seq: int, folder: str
) -> bool:
    """Shared sequence/live-holder check, after resolving persistence authority."""
    path = Path(os.path.normpath(folder))
    try:
        relative = path.relative_to(workspace_anchor(project_id))
    except ValueError:
        return False
    if len(relative.parts) != 2 or relative.parts[0] != definition:
        return False
    if relative.parts[1] == "history":
        return not path.is_symlink()
    match = _SESSION_FOLDER.fullmatch(relative.parts[1])
    if match is None or int(match.group(1)) >= seq:
        return False
    bound = db.execute(
        _LIVE_FOLDER_BINDER_SQL,
        (project_id, list(LIVE_SESSION_STATUSES), str(path)),
    ).fetchone()
    return bound is None


_SESSION_LOCATION_SQL = "SELECT project_id, seq_num FROM sessions WHERE id = %s"


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
    from gobby.agents.team_admission_records import team_binding
    from gobby.agents.team_lifecycle import seat_team
    from gobby.agents.team_scope import live_teams
    from gobby.storage.team_records import EPIC_MODE

    binding = seat_team(db, session_id)
    if binding is not None and binding.parent_team_id:
        binding = team_binding(db, binding.parent_team_id)
    if binding is None or binding.mode != EPIC_MODE:
        return None
    live = live_teams(db, [binding.task_id]).get(binding.task_id)
    if live is None or live.team_id != binding.team_id:
        return None
    return _epic_folder(db, binding)


def _checkout_root(path: Path) -> Path | None:
    """``path`` when it is spelled canonically and git reports it as its own toplevel.

    A symlinked or aliased spelling (macOS /var vs /private/var, or a link inside a seat
    folder) is refused, so only the one canonical location grants the checkout.
    """
    from gobby.utils.git import run_git_command

    if path.resolve() != path:
        return None
    checkout = run_git_command(["git", "rev-parse", "--show-toplevel"], path)
    if checkout is None or Path(checkout).resolve() != path:
        return None
    return path


def assigned_run_checkout(db: HubDatabase, session_id: str) -> Path | None:
    """The run's registered checkout, canonical and git-verified, wherever it lives."""
    from gobby.storage.worktrees import LocalWorktreeManager

    worktree = LocalWorktreeManager(db).get_for_session_run(session_id)
    if worktree is None:
        return None
    return _checkout_root(Path(worktree.worktree_path))


def assigned_task_worktree(db: HubDatabase, session_id: str) -> Path | None:
    """Resolve a run's registered task checkout under the workspaces root, never its container."""
    path = assigned_run_checkout(db, session_id)
    if path is None or not path.is_relative_to(workspaces_root()):
        return None
    return path


def _team_checkout(db: HubDatabase, session_id: str) -> Path | None:
    """The seat's team checkout, when the worktree table registers it as a checkout."""
    from gobby.agents.team_lifecycle import seat_team
    from gobby.storage.worktrees import LocalWorktreeManager

    team = seat_team(db, session_id)
    if team is None or team.worktree_path is None:
        return None
    if LocalWorktreeManager(db).get_by_path(team.worktree_path) is None:
        return None
    return _checkout_root(Path(team.worktree_path))


def spawn_workspace_write_paths(
    db: HubDatabase, session_id: str, definition: str | None
) -> list[str]:
    """The seat's own folder, epic folder, and its registered merge checkouts.

    Acquire persistent ownership before granting access, including before SessionStart.
    Other definitions reuse a bound folder or derive one from the sequence number. The run's
    registered checkout goes to every seat under the workspaces root, and to a base-merge
    seat wherever it lives. The team's registered checkout goes only to a base-merge seat.
    Neither reaches a pod, a parent, or the containing directory.
    """
    from gobby.workflows.agent_resolver import session_has_capability
    from gobby.workflows.base_merge_commits import CAPABILITY
    from gobby.workflows.state_manager import SessionVariableManager

    location = db.execute(_SESSION_LOCATION_SQL, (session_id,)).fetchone()
    if location is None or location["project_id"] is None or location["seq_num"] is None:
        return []
    bound = (
        SessionVariableManager(db)
        .get_variable_subset(session_id, [WORKSPACE_DIR_VARIABLE])
        .get(WORKSPACE_DIR_VARIABLE)
    )
    folders: list[Path] = []
    if definition and definition != "default":
        from gobby.storage.definitions import AgentDefinitionManager

        row = AgentDefinitionManager(db).get_by_name(definition, location["project_id"])
        persistent = row is not None and row.definition_json.get("persistent_workspace") is True
        if persistent:
            from gobby.agents.persistent_workspaces import bind_persistent_workspace

            with db.transaction():
                folder = bind_persistent_workspace(
                    db, session_id, location["project_id"], definition, location["seq_num"]
                )
                SessionVariableManager(db).merge_variables(
                    session_id,
                    {
                        WORKSPACE_DIR_VARIABLE: str(folder),
                        WORKSPACE_PERSISTENT_VARIABLE: True,
                        WORKSPACE_DEFINITION_VARIABLE: definition,
                    },
                )
        else:
            folder = (
                Path(bound)
                if isinstance(bound, str) and bound
                else session_workspace_dir(location["project_id"], definition, location["seq_num"])
            )
        folders.append(folder)
        epic = _seat_epic_folder(db, session_id)
        if epic is not None:
            folders.append(epic)
    capable = session_has_capability(db, session_id, CAPABILITY)
    worktree = assigned_run_checkout(db, session_id)
    if worktree is not None and (capable or worktree.is_relative_to(workspaces_root())):
        folders.append(worktree)
    if capable:
        team = _team_checkout(db, session_id)
        if team is not None:
            folders.append(team)
    return [str(folder) for folder in dict.fromkeys(folders)]


def grant_seat_workspace_paths(
    config: SandboxConfig, db: HubDatabase, session_id: str, definition: str | None
) -> SandboxConfig:
    """Recompute launch authority from the session and stored definition, including resume.

    Read/write scope is private runtime state, never authority carried by a saved
    policy snapshot. A new session need not have received SessionStart variables yet.
    """
    from gobby.storage.definitions import AgentDefinitionManager

    granted = spawn_workspace_write_paths(db, session_id, definition)
    # Workspace scope holds only folders under the root. A registered checkout is a write
    # grant alone, because restrict_workspace_paths judges scope paths against the root.
    folders = [path for path in granted if Path(path).is_relative_to(workspaces_root())]
    reads = list(folders)
    location = db.execute(_SESSION_LOCATION_SQL, (session_id,)).fetchone()
    if (
        definition
        and definition != "default"
        and location is not None
        and location["project_id"] is not None
    ):
        anchor = workspace_anchor(location["project_id"])
        reads.append(str(anchor / "README.md"))
        row = (
            AgentDefinitionManager(db).get_by_name(definition, location["project_id"])
            if definition
            else None
        )
        body = row.definition_json if row is not None else {}
        if holds_capability(body.get("capabilities", []), "promote_workspace_binaries"):
            granted.append(str(get_gobby_home() / "bin"))
        if body.get("read_all_agent_workspaces") is True:
            reads = [str(workspaces_root())]
        elif body.get("persistent_workspace") is True and definition and location["seq_num"]:
            directory = anchor / definition
            # Validate the identity before enumerating this one definition's folders.
            config.with_workspace_scope([str(directory)], []).restrict_workspace_paths([], [])
            history = directory / "history"
            if history.is_dir() and not history.is_symlink():
                reads.append(str(history))
    scoped = config.with_workspace_scope(reads, folders)
    # Reject aliasing bindings before launch creates any directories in them.
    scoped.restrict_workspace_paths([], [])
    # SRT's Linux bind grants require existing paths, including persona launches
    # that do not pass through the spawned-agent directory preparation step.
    for folder in granted:
        Path(folder).mkdir(parents=True, exist_ok=True)
    root = workspaces_root().resolve()
    retained = [
        path
        for path in config.extra_write_paths
        if not Path(path).expanduser().resolve().is_relative_to(root)
    ]
    return scoped.model_copy(
        update={"extra_write_paths": list(dict.fromkeys([*retained, *granted]))}
    )


def bind_epic_workspace(db: HubDatabase, session_id: str) -> Path | None:
    """Bind the epic folder for a seat run of a live epic team; None outside one.

    Every seat run of the team binds the same folder, sub-worktree developers included. A
    single-task team has no epic folder, and a team whose epic has closed names none.
    """
    from gobby.workflows.state_manager import SessionVariableManager

    variables = SessionVariableManager(db)
    definition = variables.get_variables(session_id).get("_agent_type")
    if not definition or definition == "default":
        variables.merge_variables(session_id, {WORKSPACE_EPIC_DIR_VARIABLE: None})
        return None
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
