"""Per-project agent workspaces: one daemon-committed Git repository under Gobby home.

Layout (addendum Decision 27, P9): ``<gobby_home>/agents_workspaces/<project_id>/`` is the
work tree, ``<gobby_home>/agents_workspaces/<project_id>.git/`` its daemon-only Git directory.
Each session writes ``<definition>/session-<seq>/``; an epic team shares ``epics/epic-<seq>/``.

Behavior-free shape for the addendum's failing tests; #24824 implements it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

WORKSPACE_DIR_VARIABLE = "_agent_workspace_dir"
WORKSPACE_EPIC_DIR_VARIABLE = "_agent_workspace_epic_dir"
WORKSPACE_PERSISTENT_VARIABLE = "_agent_workspace_persistent"


class CronRegistration(Protocol):
    def register_handler(self, name: str, handler: Any) -> None:
        """Register a cron handler by name."""
        ...


def workspaces_root() -> Path:
    """``<gobby_home>/agents_workspaces``, honoring ``$GOBBY_HOME``."""
    raise NotImplementedError


def workspace_anchor(project_id: str) -> Path:
    """The project's workspace work tree."""
    raise NotImplementedError


def workspace_git_dir(project_id: str) -> Path:
    """The project's daemon-only Git directory, a sibling of the work tree."""
    raise NotImplementedError


def ensure_workspace(project_id: str) -> Path:
    """Create the work tree, its explicit Git directory and the README; return the anchor."""
    raise NotImplementedError


async def commit_workspace(project_id: str, trigger: str) -> str | None:
    """Commit a dirty work tree under the project's lock; None when nothing changed."""
    raise NotImplementedError


def schedule_workspace_commit(project_id: str | None, trigger: str) -> None:
    """Schedule a commit and return at once; a failure is logged, never raised."""
    raise NotImplementedError


async def drain_workspace_commits() -> None:
    """Await every scheduled commit, as daemon shutdown does before exit."""
    raise NotImplementedError


def register_agent_workspace_cron(
    *, cron_storage: CronJobStorage, cron_executor: CronRegistration
) -> int:
    """Register the five-minute workspace commit handler and its system job."""
    raise NotImplementedError


async def run_workspace_cron(job: CronJob) -> str:
    """The cron handler body: commit every dirty project workspace."""
    raise NotImplementedError


def session_workspace_dir(project_id: str, definition: str | None, session_seq: int) -> Path:
    """``<anchor>/<definition or default>/session-<seq>``."""
    raise NotImplementedError


def epic_workspace_dir(project_id: str, epic_seq: int) -> Path:
    """``<anchor>/epics/epic-<seq>``."""
    raise NotImplementedError


def agent_workspace_write_dirs(variables: Mapping[str, Any]) -> list[str]:
    """The session folder and, when bound, the epic folder."""
    raise NotImplementedError


def predecessor_folder_readable(sessions: SessionManager, caller_id: str, folder: str) -> bool:
    """Whether a persistent caller may read ``folder`` as an ended earlier lineage's folder."""
    raise NotImplementedError


def bind_session_workspace(db: HubDatabase, session_id: str) -> Path | None:
    """Bind the session folder at activation unless a clear successor inherited one."""
    raise NotImplementedError


def bind_epic_workspace(db: HubDatabase, session_id: str) -> Path | None:
    """Bind the epic folder for a seat of a live epic-mode team; clear it otherwise."""
    raise NotImplementedError
