"""Resolve daemon-facing wiki scopes into gwiki CLI scope arguments."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.storage.project_checkouts import require_root
from gobby.storage.projects import (
    CHECKOUT_FREE_PROJECT_IDS,
    PERSONAL_PROJECT_ID,
    LocalProjectManager,
)
from gobby.storage.workspace_machine_scope import require_local_machine_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


PROJECT_SCOPE_PREFIX = "project:"
TOPIC_SCOPE_PREFIX = "topic:"
RESERVED_TOPIC_NAMES = frozenset({"personal", "_personal", "wiki"})
PERSONAL_SENTINELS = frozenset({PERSONAL_PROJECT_ID, "_personal"})


class WikiScopeResolutionError(ValueError):
    """Raised when a daemon wiki scope cannot be resolved safely."""


@dataclass(frozen=True)
class ResolvedWikiScope:
    identity: str | None
    project_id: str | None = None
    project_root: Path | None = None
    topic: str | None = None


def project_scope(project_id: str) -> str:
    value = project_id.strip()
    if not value:
        raise WikiScopeResolutionError("project scope requires a project id")
    if value.startswith(PROJECT_SCOPE_PREFIX):
        value = value.removeprefix(PROJECT_SCOPE_PREFIX).strip()
    if not value:
        raise WikiScopeResolutionError("project scope requires a project id")
    return f"{PROJECT_SCOPE_PREFIX}{value}"


def topic_scope(topic: str) -> str:
    value = topic.strip()
    if not value:
        raise WikiScopeResolutionError("topic scope requires a topic name")
    if value.startswith(TOPIC_SCOPE_PREFIX):
        value = value.removeprefix(TOPIC_SCOPE_PREFIX).strip()
    if not value:
        raise WikiScopeResolutionError("topic scope requires a topic name")
    if value in RESERVED_TOPIC_NAMES:
        raise WikiScopeResolutionError(f"topic name `{value}` is reserved")
    return f"{TOPIC_SCOPE_PREFIX}{value}"


def owner_wiki_home() -> Path:
    """Return `<files_home>/wiki` on the local files owner."""
    from gobby.paths import require_files_home

    return require_files_home() / "wiki"


def owner_personal_wiki_root() -> Path:
    """Return `<files_home>/wiki/personal` on the local files owner."""
    return owner_wiki_home() / "personal"


def _is_personal_sentinel(value: str) -> bool:
    stripped = value.strip()
    if stripped.startswith(PROJECT_SCOPE_PREFIX):
        stripped = stripped.removeprefix(PROJECT_SCOPE_PREFIX).strip()
    return stripped in PERSONAL_SENTINELS


def _require_owner_files_home_if_local() -> None:
    from gobby.paths import FilesHomeError, get_files_home, require_files_home

    if get_files_home() is None:
        return
    try:
        require_files_home()
    except FilesHomeError as exc:
        raise WikiScopeResolutionError(str(exc)) from exc


def _personal_scope() -> ResolvedWikiScope:
    from gobby.paths import get_files_home, require_files_home

    _require_owner_files_home_if_local()
    project_root = require_files_home() / "_personal" if get_files_home() is not None else None
    return ResolvedWikiScope(
        identity=f"{PROJECT_SCOPE_PREFIX}{PERSONAL_PROJECT_ID}",
        project_id=PERSONAL_PROJECT_ID,
        project_root=project_root,
    )


def normalize_scope_identity(scope: str, *, default_project_id: str | None = None) -> str:
    value = scope.strip()
    if not value:
        if default_project_id is not None:
            return project_scope(default_project_id)
        raise WikiScopeResolutionError("wiki scope cannot be empty")
    if value.startswith(TOPIC_SCOPE_PREFIX):
        return topic_scope(value)
    if value.startswith(PROJECT_SCOPE_PREFIX):
        return project_scope(value)
    return project_scope(value)


async def resolve_wiki_scope(
    db: HubDatabase | None,
    *,
    project: str | None = None,
    topic: str | None = None,
    default_project_id: str | None = None,
) -> ResolvedWikiScope:
    if project is not None and topic is not None:
        raise WikiScopeResolutionError("Provide project or topic scope, not both")

    if topic is not None:
        identity = topic_scope(topic)
        _require_owner_files_home_if_local()
        return ResolvedWikiScope(
            identity=identity,
            topic=identity.removeprefix(TOPIC_SCOPE_PREFIX),
        )

    project_ref = project if project is not None else default_project_id
    if project_ref is None:
        return ResolvedWikiScope(identity=None)
    if _is_personal_sentinel(project_ref):
        return _personal_scope()

    return await resolve_scope_identity(db, project_ref, require_project_root=True)


async def resolve_scope_identity(
    db: HubDatabase | None,
    scope: str,
    *,
    require_project_root: bool = True,
) -> ResolvedWikiScope:
    identity = normalize_scope_identity(scope)
    if identity.startswith(TOPIC_SCOPE_PREFIX):
        _require_owner_files_home_if_local()
        return ResolvedWikiScope(
            identity=identity,
            topic=identity.removeprefix(TOPIC_SCOPE_PREFIX),
        )

    project_id = identity.removeprefix(PROJECT_SCOPE_PREFIX)
    if _is_personal_sentinel(project_id) or project_id in CHECKOUT_FREE_PROJECT_IDS:
        if _is_personal_sentinel(project_id):
            return _personal_scope()
        return ResolvedWikiScope(identity=identity, project_id=project_id)
    project_root = await resolve_project_root(db, project_id) if db is not None else None
    if require_project_root and project_root is None:
        raise WikiScopeResolutionError("project-scoped wiki calls require a database")
    return ResolvedWikiScope(
        identity=identity,
        project_id=project_id,
        project_root=project_root,
    )


async def resolve_project_root(db: HubDatabase | None, project_id: str) -> Path:
    if db is None:
        raise WikiScopeResolutionError("project-scoped wiki calls require a database")

    return await asyncio.to_thread(_resolve_project_root_sync, db, project_id)


def _resolve_project_root_sync(db: HubDatabase, project_id: str) -> Path:
    if project_id in CHECKOUT_FREE_PROJECT_IDS:
        raise WikiScopeResolutionError(
            f"checkout-free sentinel {project_id} cannot resolve a wiki project root"
        )
    project = LocalProjectManager(db).get(project_id)
    if project is None:
        raise WikiScopeResolutionError(f"Unknown project id: {project_id}")
    machine_id = require_local_machine_id(
        None, resource_kind="project_checkout", resource_id=project_id
    )
    return Path(require_root(db, project_id, machine_id)).expanduser().resolve()
