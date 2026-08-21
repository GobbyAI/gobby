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

import logging
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Protocol, cast

from gobby.ai.embedding_catalog import (
    EmbeddingModelSpec,
    catalog_model_for_provider,
    get_spec_or_raise,
)
from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_CATALOG_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
    AI_EMBEDDING_QUERY_PREFIX_KEY,
    EMBEDDING_SWITCH_COMPLETED_KEY,
    EMBEDDING_SWITCH_JOURNAL_KEY,
)
from gobby.memory.collection_names import CollectionNameResolver
from gobby.storage.config_mutations import EmbeddingConfigMutationBlocked

logger = logging.getLogger(__name__)


class EmbeddingSwitchLifecycleStore(Protocol):
    """Storage contract for the private embedding-switch journal."""

    def get_internal_lifecycle(self, key: str) -> Any | None: ...

    def set_internal_lifecycle(self, key: str, value: Any) -> None: ...

    def delete_internal_lifecycle(self, key: str, run_id: str) -> bool: ...

    def complete_embedding_switch(
        self,
        run_id: str,
        entries: dict[str, Any],
        completed_key: str,
        completed_record: dict[str, object],
    ) -> int: ...

    def read_snapshot(self) -> Any: ...


class EmbeddingSwitchSnapshot(Protocol):
    @property
    def revision(self) -> int: ...

    @property
    def values(self) -> Mapping[str, object]: ...

    @property
    def overrides(self) -> Mapping[str, object]: ...

    @property
    def row_revisions(self) -> Mapping[str, int]: ...


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
    physical_names: dict[str, str] = field(default_factory=dict)
    caught_up_watermark: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return cast(dict[str, object], asdict(self))

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> SwitchJournal:
        d = dict(data)
        d.setdefault("old_physical_names", {})
        d.setdefault("physical_names", {})
        d.setdefault("caught_up_watermark", 0)
        return SwitchJournal(**cast(Any, d))


@dataclass(frozen=True, slots=True)
class CompletedSwitchRecord:
    """Durable proof binding one structural revision to physical collections."""

    run_id: str
    committed_revision: int
    physical_names: dict[str, str]
    old_physical_names: dict[str, str]
    caught_up_watermark: int
    catalog_key: str
    target_dim: int
    target_model: str
    target_query_prefix: str | None
    target_api_base: str | None

    def to_dict(self) -> dict[str, object]:
        return cast(dict[str, object], asdict(self))

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> CompletedSwitchRecord:
        return CompletedSwitchRecord(**cast(Any, dict(data)))


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


def _read_journal(config_store: EmbeddingSwitchLifecycleStore) -> SwitchJournal | None:
    """Read the current switch journal from ConfigStore."""
    raw = config_store.get_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SwitchJournalStateError(
            f"Invalid embedding switch journal type: {type(raw).__name__}"
        )
    try:
        return SwitchJournal.from_dict(raw)
    except Exception as exc:
        logger.warning("Failed to parse switch journal", exc_info=True)
        raise SwitchJournalStateError("Invalid embedding switch journal") from exc


def _write_journal(
    config_store: EmbeddingSwitchLifecycleStore,
    journal: SwitchJournal,
) -> None:
    """Write the switch journal to ConfigStore."""
    config_store.set_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, journal.to_dict())


def _delete_journal(
    config_store: EmbeddingSwitchLifecycleStore,
    journal: SwitchJournal,
) -> None:
    """Delete the switch journal from ConfigStore."""
    config_store.delete_internal_lifecycle(EMBEDDING_SWITCH_JOURNAL_KEY, journal.run_id)


def get_switch_status(config_store: EmbeddingSwitchLifecycleStore) -> SwitchJournal | None:
    """Return the current switch journal, or None if no active switch."""
    return _read_journal(config_store)


def abort_switch(config_store: EmbeddingSwitchLifecycleStore) -> SwitchJournal | None:
    """Mark the current switch as awaiting staged-artifact cleanup."""
    journal = _read_journal(config_store)
    if journal is None:
        return None
    return advance_phase(config_store, journal, PHASE_ABORTED)


def complete_aborted_switch(
    config_store: EmbeddingSwitchLifecycleStore,
    journal: SwitchJournal,
) -> None:
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
    target_model: str | None = None,
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
        target_model=target_model,
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
    target_model: str | None = None,
) -> tuple[SwitchJournal, EmbeddingModelSpec]:
    existing = _read_journal(config_store)
    if existing is not None and existing.phase not in (PHASE_ABORTED, PHASE_GC):
        raise SwitchAlreadyActiveError(existing)

    spec = get_spec_or_raise(catalog_key)
    if target_model is None:
        target_model = catalog_model_for_provider(spec, provider)
    if target_model is None:
        raise ValueError(
            f"Provider {provider!r} requires the target model to be resolved "
            "before opening a switch journal"
        )
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


def advance_phase(
    config_store: EmbeddingSwitchLifecycleStore,
    journal: SwitchJournal,
    phase: str,
) -> SwitchJournal:
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
    config_store: EmbeddingSwitchLifecycleStore,
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


def persist_journal(
    config_store: EmbeddingSwitchLifecycleStore,
    journal: SwitchJournal,
) -> SwitchJournal:
    """Persist journal metadata without changing phase."""
    from datetime import UTC, datetime

    journal.updated_at = datetime.now(UTC).isoformat()
    _write_journal(config_store, journal)
    return journal


def complete_switch(
    config_store: EmbeddingSwitchLifecycleStore,
    journal: SwitchJournal,
) -> CompletedSwitchRecord:
    """Commit structural values and durable completion proof in one revision."""
    journal.phase = PHASE_GC
    physical_names = journal.physical_names or build_physical_names(journal)
    record = CompletedSwitchRecord(
        run_id=journal.run_id,
        committed_revision=0,
        physical_names=dict(physical_names),
        old_physical_names=dict(journal.old_physical_names),
        caught_up_watermark=journal.caught_up_watermark,
        catalog_key=journal.catalog_key,
        target_dim=journal.target_dim,
        target_model=journal.target_model,
        target_query_prefix=journal.target_query_prefix,
        target_api_base=journal.target_api_base,
    )
    complete = getattr(type(config_store), "complete_embedding_switch", None)
    if complete is None:
        _write_journal(config_store, journal)
        _delete_journal(config_store, journal)
        return record
    entries = {
        AI_EMBEDDING_MODEL_KEY: journal.target_model,
        AI_EMBEDDING_DIM_KEY: journal.target_dim,
        AI_EMBEDDING_CATALOG_KEY: journal.catalog_key,
        AI_EMBEDDING_QUERY_PREFIX_KEY: journal.target_query_prefix,
        AI_EMBEDDING_API_BASE_KEY: journal.target_api_base,
    }
    revision = config_store.complete_embedding_switch(
        journal.run_id,
        entries,
        EMBEDDING_SWITCH_COMPLETED_KEY,
        record.to_dict(),
    )
    return replace(record, committed_revision=revision)


def load_completed_switch(
    config_store: EmbeddingSwitchLifecycleStore,
) -> CompletedSwitchRecord:
    """Load the durable completed-switch proof after journal cleanup."""
    raw = config_store.get_internal_lifecycle(EMBEDDING_SWITCH_COMPLETED_KEY)
    if not isinstance(raw, dict):
        raise SwitchJournalStateError("Completed embedding switch record is missing")
    try:
        return CompletedSwitchRecord.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise SwitchJournalStateError("Invalid completed embedding switch record") from exc


def managed_embedding_projection(snapshot: EmbeddingSwitchSnapshot) -> dict[str, object]:
    """Resolve the verified managed projection from one config snapshot.

    Also surfaces an in-flight switch journal's physical collection names so
    in-memory consumers (VectorStore projection targets) can dual-project
    without a per-operation database read. A malformed journal is skipped —
    the journal is transient scratch state, so it never blocks reconciles."""
    managed: dict[str, object] = {}
    raw_journal = snapshot.overrides.get(EMBEDDING_SWITCH_JOURNAL_KEY)
    if isinstance(raw_journal, Mapping):
        physical_names = raw_journal.get("physical_names")
        if isinstance(physical_names, Mapping):
            names = {
                str(kind): name
                for kind, name in physical_names.items()
                if isinstance(name, str) and name
            }
            if names:
                managed[EMBEDDING_SWITCH_JOURNAL_KEY] = names
    raw = snapshot.overrides.get(EMBEDDING_SWITCH_COMPLETED_KEY)
    if raw is None:
        return managed
    if not isinstance(raw, Mapping):
        raise SwitchJournalStateError("Completed embedding switch record is invalid")
    record = CompletedSwitchRecord.from_dict(raw)
    managed[EMBEDDING_SWITCH_COMPLETED_KEY] = _verify_completed_record_rows(snapshot, record)
    return managed


_COMPLETED_RECORD_ROW_KEYS = (
    AI_EMBEDDING_MODEL_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_CATALOG_KEY,
    EMBEDDING_SWITCH_COMPLETED_KEY,
)


def _expected_completed_values(record: CompletedSwitchRecord) -> dict[str, object]:
    return {
        AI_EMBEDDING_MODEL_KEY: record.target_model,
        AI_EMBEDDING_DIM_KEY: record.target_dim,
        AI_EMBEDDING_CATALOG_KEY: record.catalog_key,
        AI_EMBEDDING_QUERY_PREFIX_KEY: record.target_query_prefix,
        AI_EMBEDDING_API_BASE_KEY: record.target_api_base,
    }


def _verify_completed_record_rows(
    snapshot: EmbeddingSwitchSnapshot,
    record: CompletedSwitchRecord,
) -> CompletedSwitchRecord:
    """Verify a completed record against any later coherent snapshot.

    Sound because the five values must still match and managed structural rows
    cannot be externally rewritten. Rows unchanged by the completing patch may
    retain an older revision; a later tampering write would exceed committed.
    """
    committed = record.committed_revision
    if not 0 < committed <= snapshot.revision:
        raise SwitchJournalStateError(
            f"Completed embedding switch revision {committed} is outside (0, {snapshot.revision}]"
        )
    expected = _expected_completed_values(record)
    if any(snapshot.values.get(key) != value for key, value in expected.items()):
        raise SwitchJournalStateError("Completed embedding switch values do not match storage")
    if any(snapshot.row_revisions.get(key, 0) > committed for key in _COMPLETED_RECORD_ROW_KEYS):
        raise SwitchJournalStateError("Completed embedding switch rows postdate the commit")
    return record


def resolve_collection_names(journal: SwitchJournal) -> CollectionNameResolver:
    """Return a CollectionNameResolver for the switch run."""
    return CollectionNameResolver()


def build_physical_names(journal: SwitchJournal) -> dict[str, str]:
    """Return {kind: physical_name} for all daemon-managed collections."""
    if journal.physical_names:
        return dict(journal.physical_names)
    resolver = CollectionNameResolver()
    return {kind: resolver.physical_name(kind, journal.run_id) for kind in resolver.kinds}


def active_alias_names() -> dict[str, str]:
    """Return {kind: active_alias} for all daemon-managed collections."""
    resolver = CollectionNameResolver()
    return {kind: resolver.active_alias(kind) for kind in resolver.kinds}
