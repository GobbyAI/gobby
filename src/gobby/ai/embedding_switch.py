"""Staged, two-phase, resumable embedding model switch.

Implements a state machine that safely switches the active embedding model
across all daemon-managed Qdrant collections. The old index stays live until
a verified flip, so a mid-run failure never leaves a half-migrated active index.

Phases:
    staging → building → flipping → active → gc

A switch journal (stored in ConfigStore) tracks the run state so
``--status``/``--resume``/``--abort`` can recover from any phase.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from gobby.ai.embedding_catalog import EmbeddingModelSpec, get_spec_or_raise
from gobby.config.embedding_keys import EMBEDDING_SWITCH_JOURNAL_KEY
from gobby.memory.collection_names import CollectionNameResolver
from gobby.storage.config_store import EmbeddingConfigMutationBlocked

logger = logging.getLogger(__name__)

# Phase names in order.
PHASE_STAGING = "staging"
PHASE_BUILDING = "building"
PHASE_FLIPPING = "flipping"
PHASE_ACTIVE = "active"
PHASE_GC = "gc"
PHASE_ABORTED = "aborted"

_VALID_PHASES = frozenset(
    {
        PHASE_STAGING,
        PHASE_BUILDING,
        PHASE_FLIPPING,
        PHASE_ACTIVE,
        PHASE_GC,
        PHASE_ABORTED,
    }
)
_TARGET_API_BASE_UNSET = object()


@dataclass
class SwitchJournal:
    """Journal entry tracking the state of an embedding switch run."""

    run_id: str
    catalog_key: str
    target_dim: int
    target_model: str
    target_query_prefix: str | None
    target_api_base: str | None
    provider: str
    phase: str
    started_at: str
    updated_at: str
    old_catalog_id: str | None = None
    old_dim: int | None = None
    old_physical_names: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @staticmethod
    def from_json(data: str) -> SwitchJournal:
        d = json.loads(data)
        d.setdefault("old_physical_names", {})
        return SwitchJournal(**d)


class SwitchError(RuntimeError):
    """Raised when a switch operation fails."""


class SwitchJournalStateError(SwitchError):
    """Raised when the persisted switch journal cannot be trusted."""


class SwitchAlreadyActiveError(SwitchError):
    """Raised when a switch is already in progress."""

    def __init__(self, journal: SwitchJournal) -> None:
        self.journal = journal
        super().__init__(
            f"Embedding switch already active (run_id={journal.run_id}, "
            f"phase={journal.phase}). Use --status, --resume, or --abort."
        )


def _read_journal(config_store: Any) -> SwitchJournal | None:
    """Read the current switch journal from ConfigStore."""
    internal_get = (
        config_store.get_internal_lifecycle
        if callable(getattr(type(config_store), "get_internal_lifecycle", None))
        else None
    )
    raw = (
        internal_get(EMBEDDING_SWITCH_JOURNAL_KEY)
        if callable(internal_get)
        else config_store.get(EMBEDDING_SWITCH_JOURNAL_KEY)
    )
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise SwitchJournalStateError(
            f"Invalid embedding switch journal type: {type(raw).__name__}"
        )
    try:
        return SwitchJournal.from_json(raw)
    except Exception as exc:
        logger.warning("Failed to parse switch journal", exc_info=True)
        raise SwitchJournalStateError("Invalid embedding switch journal") from exc


def _write_journal(config_store: Any, journal: SwitchJournal) -> None:
    """Write the switch journal to ConfigStore."""
    internal_set = (
        config_store.set_internal_lifecycle
        if callable(getattr(type(config_store), "set_internal_lifecycle", None))
        else None
    )
    if callable(internal_set):
        internal_set(EMBEDDING_SWITCH_JOURNAL_KEY, journal.to_json())
    else:
        config_store.set(
            EMBEDDING_SWITCH_JOURNAL_KEY,
            journal.to_json(),
            source="embedding_switch",
        )


def _delete_journal(config_store: Any, journal: SwitchJournal) -> None:
    """Delete the switch journal from ConfigStore."""
    internal_delete = (
        config_store.delete_internal_lifecycle
        if callable(getattr(type(config_store), "delete_internal_lifecycle", None))
        else None
    )
    if callable(internal_delete):
        internal_delete(EMBEDDING_SWITCH_JOURNAL_KEY, journal.run_id)
    else:
        config_store.delete(EMBEDDING_SWITCH_JOURNAL_KEY)


def get_switch_status(config_store: Any) -> SwitchJournal | None:
    """Return the current switch journal, or None if no active switch."""
    return _read_journal(config_store)


def abort_switch(config_store: Any) -> SwitchJournal | None:
    """Mark the current switch as awaiting staged-artifact cleanup."""
    journal = _read_journal(config_store)
    if journal is None:
        return None
    return advance_phase(config_store, journal, PHASE_ABORTED)


def complete_aborted_switch(config_store: Any, journal: SwitchJournal) -> None:
    """Delete an aborted journal only after all staged artifacts are gone."""
    if journal.phase != PHASE_ABORTED:
        raise SwitchJournalStateError("Only an aborted switch journal may be completed")
    _delete_journal(config_store, journal)


@dataclass
class SwitchResult:
    """Result of a switch operation."""

    success: bool
    phase: str
    run_id: str
    catalog_key: str
    target_dim: int
    error: str | None = None
    message: str | None = None


def start_switch(
    config_store: Any,
    catalog_key: str,
    provider: str,
    *,
    current_dim: int | None = None,
    current_catalog_id: str | None = None,
    current_api_base: str | None = None,
    target_api_base: str | None | object = _TARGET_API_BASE_UNSET,
) -> tuple[SwitchJournal, EmbeddingModelSpec]:
    """Open a switch journal and return it with the resolved spec.

    Raises SwitchAlreadyActiveError if a switch is already in progress.
    Raises ValueError if the catalog key is unknown.
    """
    return _start_switch_unlocked(
        config_store,
        catalog_key,
        provider,
        current_dim=current_dim,
        current_catalog_id=current_catalog_id,
        current_api_base=current_api_base,
        target_api_base=target_api_base,
    )


def _start_switch_unlocked(
    config_store: Any,
    catalog_key: str,
    provider: str,
    *,
    current_dim: int | None,
    current_catalog_id: str | None,
    current_api_base: str | None,
    target_api_base: str | None | object,
) -> tuple[SwitchJournal, EmbeddingModelSpec]:
    existing = _read_journal(config_store)
    if existing is not None and existing.phase not in (PHASE_ABORTED, PHASE_GC):
        raise SwitchAlreadyActiveError(existing)

    spec = get_spec_or_raise(catalog_key)
    if provider == "ollama":
        target_model = spec.ollama_tag
    elif provider == "lmstudio":
        target_model = spec.lmstudio_ref
    else:
        target_model = spec.key
    resolved_target_api_base = (
        current_api_base
        if target_api_base is _TARGET_API_BASE_UNSET
        else target_api_base
        if isinstance(target_api_base, str)
        else None
    )

    run_id = f"{spec.dim}-{uuid.uuid4().hex[:8]}"
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    journal = SwitchJournal(
        run_id=run_id,
        catalog_key=catalog_key,
        target_dim=spec.dim,
        target_model=target_model,
        target_query_prefix=spec.query_prefix,
        target_api_base=resolved_target_api_base,
        provider=provider,
        phase=PHASE_STAGING,
        started_at=now,
        updated_at=now,
        old_catalog_id=current_catalog_id,
        old_dim=current_dim,
    )
    try:
        _write_journal(config_store, journal)
    except EmbeddingConfigMutationBlocked as exc:
        winner = _read_journal(config_store)
        if winner is not None:
            raise SwitchAlreadyActiveError(winner) from exc
        raise
    return journal, spec


def advance_phase(config_store: Any, journal: SwitchJournal, phase: str) -> SwitchJournal:
    """Advance the journal to a new phase and persist it."""
    if phase not in _VALID_PHASES:
        raise ValueError(f"Invalid phase: {phase}")
    from datetime import UTC, datetime

    journal.phase = phase
    journal.updated_at = datetime.now(UTC).isoformat()
    journal.error = None
    _write_journal(config_store, journal)
    return journal


def record_switch_error(
    config_store: Any,
    journal: SwitchJournal,
    error: str,
    *,
    phase: str | None = None,
) -> SwitchJournal:
    """Persist a resumable switch error on the journal."""
    from datetime import UTC, datetime

    if phase is not None:
        if phase not in _VALID_PHASES:
            raise ValueError(f"Invalid phase: {phase}")
        journal.phase = phase
    journal.error = error
    journal.updated_at = datetime.now(UTC).isoformat()
    _write_journal(config_store, journal)
    return journal


def persist_journal(config_store: Any, journal: SwitchJournal) -> SwitchJournal:
    """Persist journal metadata without changing phase."""
    from datetime import UTC, datetime

    journal.updated_at = datetime.now(UTC).isoformat()
    _write_journal(config_store, journal)
    return journal


def complete_switch(config_store: Any, journal: SwitchJournal) -> None:
    """Mark the switch as complete and clean up the journal."""
    advance_phase(config_store, journal, PHASE_GC)
    _delete_journal(config_store, journal)


def resolve_collection_names(journal: SwitchJournal) -> CollectionNameResolver:
    """Return a CollectionNameResolver for the switch run."""
    return CollectionNameResolver()


def build_physical_names(journal: SwitchJournal) -> dict[str, str]:
    """Return {kind: physical_name} for all daemon-managed collections."""
    resolver = CollectionNameResolver()
    return {kind: resolver.physical_name(kind, journal.run_id) for kind in resolver.kinds}


def active_alias_names() -> dict[str, str]:
    """Return {kind: active_alias} for all daemon-managed collections."""
    resolver = CollectionNameResolver()
    return {kind: resolver.active_alias(kind) for kind in resolver.kinds}
