"""Revision-coherent reads for registry-backed daemon configuration."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import TypeAdapter

from gobby.config.app import DaemonConfig
from gobby.config.registry import (
    CONFIG_REGISTRY,
    ConfigPatternSpec,
    ConfigRegistry,
    ConfigSecrecy,
    RegistrySpec,
    UnknownConfigKeyError,
    config_key_secrecy,
)
from gobby.storage.hub._ambient import ambient_transaction
from gobby.storage.hub.protocol import HubDatabase, Row, Transaction
from gobby.storage.secrets import SecretStore

MAX_CONFIG_REVISION = (1 << 53) - 1


class ConfigRepositoryError(RuntimeError):
    """Base class for configuration snapshot integrity failures."""


class UnknownStoredConfigKeyError(ConfigRepositoryError):
    """A residual row is absent from the compiled configuration registry."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Unknown stored configuration key: {key}")


class TornConfigSnapshotError(ConfigRepositoryError):
    """Stored row metadata cannot belong to the captured global revision."""


@dataclass(frozen=True, slots=True)
class SecretBinding:
    """Secret reference and payload captured in one database snapshot."""

    reference: str
    plaintext: str | None


@dataclass(frozen=True, slots=True)
class ConfigReadSnapshot:
    """Complete defaults plus revisioned overrides from one PostgreSQL snapshot."""

    revision: int
    values: MappingProxyType[str, object]
    overrides: MappingProxyType[str, object]
    row_revisions: MappingProxyType[str, int]
    secret_bindings: MappingProxyType[str, SecretBinding]


def decode_config_value(key: str, raw_value: str) -> object:
    """Decode one JSON storage value while retaining the failing key."""
    try:
        return json.loads(raw_value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConfigRepositoryError(f"Invalid JSON for configuration key {key!r}") from exc


def registry_is_secret(spec: RegistrySpec, key: str) -> bool:
    """Return the registry-derived storage secrecy marker."""
    return config_key_secrecy(spec, key) is not ConfigSecrecy.NONE


class ConfigRepository:
    """Read and reconcile configuration through the compiled registry."""

    def __init__(
        self,
        db: HubDatabase,
        *,
        registry: ConfigRegistry = CONFIG_REGISTRY,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self._secret_store = secret_store

    def _secrets(self) -> SecretStore:
        if self._secret_store is None:
            self._secret_store = SecretStore(self.db)
        return self._secret_store

    def read(self, *, resolve_secrets: bool = True) -> ConfigReadSnapshot:
        """Read a complete configuration snapshot in one read-only transaction."""
        ambient = ambient_transaction(self.db)
        if ambient is not None:
            revision, rows = self._read_coherent(ambient)
            return self.snapshot_from_rows(
                ambient,
                revision,
                rows,
                resolve_secrets=resolve_secrets,
            )
        with self.db.transaction() as transaction:
            transaction.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            revision = self._read_revision(transaction)
            rows = self._read_rows(transaction)
            return self.snapshot_from_rows(
                transaction,
                revision,
                rows,
                resolve_secrets=resolve_secrets,
            )

    def read_bounded(
        self,
        *,
        resolve_secrets: bool = True,
        statement_timeout_ms: int = 5_000,
        lock_timeout_ms: int = 5_000,
    ) -> ConfigReadSnapshot:
        """Read one coherent snapshot with PostgreSQL-enforced operation bounds."""
        ambient = ambient_transaction(self.db)
        if ambient is not None:
            return self.read(resolve_secrets=resolve_secrets)
        bounded_transaction = getattr(self.db, "bounded_transaction", None)
        if bounded_transaction is None:
            return self.read(resolve_secrets=resolve_secrets)
        with bounded_transaction(
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
            repeatable_read_read_only=True,
        ) as transaction:
            revision = self._read_revision(transaction)
            rows = self._read_rows(transaction)
            return self.snapshot_from_rows(
                transaction,
                revision,
                rows,
                resolve_secrets=resolve_secrets,
            )

    def current_revision(self) -> int:
        row = self.db.fetchone("SELECT revision FROM config_state WHERE id = %s", (True,))
        return self._validated_revision(row)

    def reconcile_registry(self) -> frozenset[str]:
        """Repair derived secrecy flags and fail closed on residual keys."""
        with self.db.transaction() as transaction:
            revision = self._read_revision(transaction, lock=True)
            rows = transaction.execute(
                "SELECT key, value, is_secret, revision FROM config_store ORDER BY key FOR UPDATE"
            ).fetchall()
            repaired: set[str] = set()
            for row in rows:
                key = str(row["key"])
                spec = self._resolve(key)
                self._validate_row_revision(key, int(row["revision"]), revision)
                expected = registry_is_secret(spec, key)
                if bool(row["is_secret"]) == expected:
                    continue
                transaction.execute(
                    "UPDATE config_store SET is_secret = %s WHERE key = %s",
                    (expected, key),
                )
                repaired.add(key)
            return frozenset(repaired)

    def _read_revision(self, transaction: Transaction, *, lock: bool = False) -> int:
        suffix = " FOR UPDATE" if lock else ""
        row = transaction.execute(
            f"SELECT revision FROM config_state WHERE id = %s{suffix}",  # nosec B608
            (True,),
        ).fetchone()
        return self._validated_revision(row)

    def _read_rows(self, transaction: Transaction) -> list[Row]:
        return transaction.execute(
            "SELECT key, value, is_secret, revision FROM config_store ORDER BY key"
        ).fetchall()

    def _read_coherent(self, transaction: Transaction) -> tuple[int, list[Row]]:
        """Read the revision and rows in one statement snapshot.

        An ambient transaction may run READ COMMITTED, where two statements can
        observe different committed states; a single joined statement cannot
        tear.
        """
        rows = transaction.execute(
            """SELECT state.revision AS global_revision,
                      store.key, store.value, store.is_secret, store.revision
                 FROM config_state AS state
                 LEFT JOIN config_store AS store ON TRUE
                WHERE state.id = %s
                ORDER BY store.key""",
            (True,),
        ).fetchall()
        if not rows:
            raise ConfigRepositoryError("Configuration revision state is missing")
        revision = int(rows[0]["global_revision"])
        if not 0 <= revision <= MAX_CONFIG_REVISION:
            raise ConfigRepositoryError(
                f"Configuration revision is outside the safe domain: {revision}"
            )
        return revision, [row for row in rows if row["key"] is not None]

    def snapshot_from_rows(
        self,
        transaction: Transaction,
        revision: int,
        rows: list[Row],
        *,
        resolve_secrets: bool = True,
    ) -> ConfigReadSnapshot:
        overrides: dict[str, object] = {}
        row_revisions: dict[str, int] = {}
        bindings: dict[str, SecretBinding] = {}
        for row in rows:
            key = str(row["key"])
            spec = self._resolve(key)
            row_revision = int(row["revision"])
            self._validate_row_revision(key, row_revision, revision)
            value = decode_config_value(key, str(row["value"]))
            overrides[key] = value
            row_revisions[key] = row_revision
            if config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE:
                reference = self._secret_reference(key, value)
                name = reference.removeprefix("$secret:")
                plaintext = self._secrets().get(name) if resolve_secrets else None
                bindings[key] = SecretBinding(reference, plaintext)

        values = self._complete_values(overrides)
        return ConfigReadSnapshot(
            revision=revision,
            values=MappingProxyType(values),
            overrides=MappingProxyType(overrides),
            row_revisions=MappingProxyType(row_revisions),
            secret_bindings=MappingProxyType(bindings),
        )

    def runtime_candidate(self, overrides: dict[str, object]) -> DaemonConfig:
        """Validate a complete prospective override set with cross-field rules."""
        candidate = DaemonConfig().model_dump(mode="python", by_alias=False)
        for key, value in overrides.items():
            spec = self._resolve(key)
            if spec.source_path is None:
                continue
            if (
                config_key_secrecy(spec, key) is ConfigSecrecy.REFERENCE
                and isinstance(value, str)
                and value.startswith("$secret:")
            ):
                # Runtime consumers (FalkorDB, Qdrant, generation endpoints)
                # need plaintext; stored rows, snapshots, and API surfaces
                # keep the reference form.
                value = self._secrets().get(value.removeprefix("$secret:"))
            path = self._runtime_path(spec, key)
            _assign_path(candidate, path, value)
        return DaemonConfig.model_validate(candidate)

    def _complete_values(self, overrides: dict[str, object]) -> dict[str, object]:
        values: dict[str, object] = {}
        for spec in self.registry.key_specs:
            if spec.has_default:
                # Stored overrides decode to JSON-plain values; defaults must
                # use the same representation or rich-typed defaults (pydantic
                # models) leak into snapshot values and break serialization.
                adapter: TypeAdapter[object] = TypeAdapter(spec.annotation)
                values[spec.key] = adapter.dump_python(
                    adapter.validate_python(copy.deepcopy(spec.default)),
                    mode="json",
                )
        values.update(overrides)
        return values

    def _resolve(self, key: str) -> RegistrySpec:
        try:
            return self.registry.resolve(key)
        except UnknownConfigKeyError as exc:
            raise UnknownStoredConfigKeyError(key) from exc

    @staticmethod
    def _runtime_path(spec: RegistrySpec, key: str) -> tuple[str, ...]:
        assert spec.source_path is not None
        path = spec.source_path.split(".")
        if not isinstance(spec, ConfigPatternSpec):
            return tuple(path)
        matched = spec.match(key)
        if matched is None:
            raise UnknownStoredConfigKeyError(key)
        path.extend(matched[name] for name in spec.placeholders)
        return tuple(path)

    @staticmethod
    def _validated_revision(row: Row | None) -> int:
        if row is None:
            raise ConfigRepositoryError("Configuration revision state is missing")
        revision = int(row["revision"])
        if not 0 <= revision <= MAX_CONFIG_REVISION:
            raise ConfigRepositoryError(
                f"Configuration revision is outside the safe domain: {revision}"
            )
        return revision

    @staticmethod
    def _validate_row_revision(key: str, row_revision: int, global_revision: int) -> None:
        if not 0 <= row_revision <= global_revision:
            raise TornConfigSnapshotError(
                f"Configuration row {key!r} revision {row_revision} exceeds global revision "
                f"{global_revision}"
            )

    @staticmethod
    def _secret_reference(key: str, value: object) -> str:
        if not isinstance(value, str) or not value.startswith("$secret:"):
            raise ConfigRepositoryError(f"Secret configuration key {key!r} has no reference")
        if not value.removeprefix("$secret:"):
            raise ConfigRepositoryError(f"Secret configuration key {key!r} has an empty reference")
        return value


def _assign_path(target: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    current = target
    for segment in path[:-1]:
        child = current.get(segment)
        if not isinstance(child, dict):
            child = {}
            current[segment] = child
        current = child
    current[path[-1]] = value


__all__ = [
    "MAX_CONFIG_REVISION",
    "ConfigReadSnapshot",
    "ConfigRepository",
    "ConfigRepositoryError",
    "SecretBinding",
    "TornConfigSnapshotError",
    "UnknownStoredConfigKeyError",
    "decode_config_value",
    "registry_is_secret",
]
