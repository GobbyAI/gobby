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
from dataclasses import asdict, dataclass
from typing import Any

from gobby.ai.embedding_catalog import EmbeddingModelSpec, get_spec_or_raise
from gobby.memory.vectorstore import CollectionNameResolver

logger = logging.getLogger(__name__)

# ConfigStore key for the switch journal (singleton — one active run at a time).
_SWITCH_JOURNAL_KEY = "ai.embeddings.switch_run"

# Phase names in order.
PHASE_STAGING = "staging"
PHASE_BUILDING = "building"
PHASE_FLIPPING = "flipping"
PHASE_ACTIVE = "active"
PHASE_GC = "gc"
PHASE_ABORTED = "aborted"

_VALID_PHASES = frozenset({
    PHASE_STAGING,
    PHASE_BUILDING,
    PHASE_FLIPPING,
    PHASE_ACTIVE,
    PHASE_GC,
    PHASE_ABORTED,
})


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
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @staticmethod
    def from_json(data: str) -> SwitchJournal:
        d = json.loads(data)
        return SwitchJournal(**d)


class SwitchError(Exception):
    """Raised when a switch operation fails."""


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
    raw = config_store.get(_SWITCH_JOURNAL_KEY)
    if raw is None or not isinstance(raw, str):
        return None
    try:
        return SwitchJournal.from_json(raw)
    except Exception as exc:
        logger.warning("Failed to parse switch journal: %s", exc)
        return None


def _write_journal(config_store: Any, journal: SwitchJournal) -> None:
    """Write the switch journal to ConfigStore."""
    config_store.set(_SWITCH_JOURNAL_KEY, journal.to_json(), source="embedding_switch")


def _delete_journal(config_store: Any) -> None:
    """Delete the switch journal from ConfigStore."""
    config_store.delete(_SWITCH_JOURNAL_KEY, source="embedding_switch")


def get_switch_status(config_store: Any) -> SwitchJournal | None:
    """Return the current switch journal, or None if no active switch."""
    return _read_journal(config_store)


def abort_switch(config_store: Any) -> SwitchJournal | None:
    """Abort the current switch run. Returns the aborted journal, or None."""
    journal = _read_journal(config_store)
    if journal is None:
        return None
    journal.phase = PHASE_ABORTED
    _write_journal(config_store, journal)
    _delete_journal(config_store)
    return journal


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
) -> tuple[SwitchJournal, EmbeddingModelSpec]:
    """Open a switch journal and return it with the resolved spec.

    Raises SwitchAlreadyActiveError if a switch is already in progress.
    Raises ValueError if the catalog key is unknown.
    """
    existing = _read_journal(config_store)
    if existing is not None and existing.phase not in (PHASE_ABORTED, PHASE_GC):
        raise SwitchAlreadyActiveError(existing)

    spec = get_spec_or_raise(catalog_key)

    run_id = f"{spec.dim}-{uuid.uuid4().hex[:8]}"
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    journal = SwitchJournal(
        run_id=run_id,
        catalog_key=catalog_key,
        target_dim=spec.dim,
        target_model=spec.ollama_tag if provider == "ollama" else spec.lmstudio_ref,
        target_query_prefix=spec.query_prefix,
        target_api_base=current_api_base,
        provider=provider,
        phase=PHASE_STAGING,
        started_at=now,
        updated_at=now,
        old_catalog_id=current_catalog_id,
        old_dim=current_dim,
    )
    _write_journal(config_store, journal)
    return journal, spec


def advance_phase(config_store: Any, journal: SwitchJournal, phase: str) -> SwitchJournal:
    """Advance the journal to a new phase and persist it."""
    if phase not in _VALID_PHASES:
        raise ValueError(f"Invalid phase: {phase}")
    from datetime import UTC, datetime

    journal.phase = phase
    journal.updated_at = datetime.now(UTC).isoformat()
    _write_journal(config_store, journal)
    return journal


def complete_switch(config_store: Any, journal: SwitchJournal) -> None:
    """Mark the switch as complete and clean up the journal."""
    advance_phase(config_store, journal, PHASE_GC)
    _delete_journal(config_store)


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
