"""Resolve daemon-facing wiki scopes into gwiki CLI scope arguments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.storage.projects import LocalProjectManager

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


PROJECT_SCOPE_PREFIX = "project:"
TOPIC_SCOPE_PREFIX = "topic:"


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
    return f"{TOPIC_SCOPE_PREFIX}{value}"


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


def resolve_wiki_scope(
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
        return ResolvedWikiScope(
            identity=identity,
            topic=identity.removeprefix(TOPIC_SCOPE_PREFIX),
        )

    project_ref = project or default_project_id
    if project_ref is None:
        return ResolvedWikiScope(identity=None)

    return resolve_scope_identity(db, project_ref, require_project_root=True)


def resolve_scope_identity(
    db: HubDatabase | None,
    scope: str,
    *,
    require_project_root: bool = True,
) -> ResolvedWikiScope:
    identity = normalize_scope_identity(scope)
    if identity.startswith(TOPIC_SCOPE_PREFIX):
        return ResolvedWikiScope(
            identity=identity,
            topic=identity.removeprefix(TOPIC_SCOPE_PREFIX),
        )

    project_id = identity.removeprefix(PROJECT_SCOPE_PREFIX)
    project_root = resolve_project_root(db, project_id) if db is not None else None
    if require_project_root and project_root is None:
        raise WikiScopeResolutionError("project-scoped wiki calls require a database")
    return ResolvedWikiScope(
        identity=identity,
        project_id=project_id,
        project_root=project_root,
    )


def resolve_project_root(db: HubDatabase | None, project_id: str) -> Path:
    if db is None:
        raise WikiScopeResolutionError("project-scoped wiki calls require a database")

    project = LocalProjectManager(db).get(project_id)
    if project is None:
        raise WikiScopeResolutionError(f"Unknown project id: {project_id}")
    if not project.repo_path:
        raise WikiScopeResolutionError(f"Project {project_id} does not have a repo path")
    return Path(project.repo_path).expanduser().resolve()
