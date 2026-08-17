"""Per-domain definition cache revisions and persistent notify half."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from threading import Lock
from typing import Any, Literal, Protocol, cast

logger = logging.getLogger(__name__)

DefinitionDomain = Literal[
    "rules",
    "agents",
    "agent_step_workflows",
    "variables",
    "pipelines",
]
DEFINITION_DOMAINS: tuple[DefinitionDomain, ...] = (
    "rules",
    "agents",
    "agent_step_workflows",
    "variables",
    "pipelines",
)
NOTIFY_CHANNEL = "gobby_definition_revisions"

_REVISION_LOCK = Lock()
_REVISIONS: dict[DefinitionDomain, int] = dict.fromkeys(DEFINITION_DOMAINS, 0)
_LISTENERS: dict[DefinitionDomain, list[Callable[[], None]]] = {
    domain: [] for domain in DEFINITION_DOMAINS
}


class RevisionExecutor(Protocol):
    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Any: ...


class RevisionReader(Protocol):
    def fetchall(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Sequence[Mapping[str, Any] | Sequence[Any]]: ...


def _require_domain(domain: str) -> DefinitionDomain:
    if domain not in DEFINITION_DOMAINS:
        raise ValueError(f"Unknown definition domain: {domain}")
    return cast(DefinitionDomain, domain)


def get_definitions_revision(domain: DefinitionDomain) -> int:
    """Return the in-process mutation revision for one definition domain."""
    checked = _require_domain(domain)
    with _REVISION_LOCK:
        return _REVISIONS[checked]


def register_revision_listener(domain: DefinitionDomain, cb: Callable[[], None]) -> None:
    """Register a process-local callback for one domain's local bumps."""
    checked = _require_domain(domain)
    with _REVISION_LOCK:
        _LISTENERS[checked].append(cb)


def bump_definitions_revision(*domains: DefinitionDomain) -> None:
    """Advance process-local counters and fire that domain's listeners."""
    unique: list[DefinitionDomain] = []
    seen: set[DefinitionDomain] = set()
    for domain in domains:
        checked = _require_domain(domain)
        if checked not in seen:
            seen.add(checked)
            unique.append(checked)

    callbacks: list[tuple[DefinitionDomain, Callable[[], None]]] = []
    with _REVISION_LOCK:
        for domain in unique:
            _REVISIONS[domain] += 1
            callbacks.extend((domain, callback) for callback in _LISTENERS[domain])

    for domain, callback in callbacks:
        try:
            callback()
        except Exception:
            logger.exception("Definition revision listener failed for domain %s", domain)


def advance_persistent_revision(conn: RevisionExecutor, *domains: DefinitionDomain) -> None:
    """Advance durable per-domain revisions and notify inside one transaction."""
    for domain in domains:
        checked = _require_domain(domain)
        cursor = conn.execute(
            """
            INSERT INTO definition_revisions (domain, revision)
            VALUES (%s, 1)
            ON CONFLICT (domain) DO UPDATE
            SET revision = definition_revisions.revision + 1,
                updated_at = NOW()
            RETURNING revision
            """,
            (checked,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"Failed to advance definition revision for {checked}")
        revision = int(row["revision"] if isinstance(row, Mapping) else row[0])
        conn.execute(
            "SELECT pg_notify(%s, %s)",
            (NOTIFY_CHANNEL, f"{checked}:{revision}"),
        )


def fetch_persistent_revisions(database: RevisionReader) -> dict[DefinitionDomain, int]:
    """Read durable revisions, defaulting unseen domains to 0."""
    observed = dict.fromkeys(DEFINITION_DOMAINS, 0)
    rows = database.fetchall("SELECT domain, revision FROM definition_revisions")
    for row in rows:
        if isinstance(row, Mapping):
            domain = str(row["domain"])
            revision = int(row["revision"])
        else:
            domain = str(row[0])
            revision = int(row[1])
        if domain in DEFINITION_DOMAINS:
            observed[cast(DefinitionDomain, domain)] = revision
    return observed


def reset_definition_revision_state() -> None:
    """Clear process-local counters and listeners. Tests only."""
    with _REVISION_LOCK:
        for domain in DEFINITION_DOMAINS:
            _REVISIONS[domain] = 0
            _LISTENERS[domain].clear()


__all__ = [
    "DEFINITION_DOMAINS",
    "NOTIFY_CHANNEL",
    "DefinitionDomain",
    "advance_persistent_revision",
    "bump_definitions_revision",
    "fetch_persistent_revisions",
    "get_definitions_revision",
    "register_revision_listener",
    "reset_definition_revision_state",
]
