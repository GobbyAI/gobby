"""Atomic bookkeeping cutover for the post-M0 migration flatten."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from gobby.storage.maintenance_epoch import (
    bind_maintenance_epoch,
    require_orchestrator_epoch,
)
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MIGRATION_LOCK_SQL,
    baseline_checksum,
)

_BOOKKEEPING_VERSION = 354
_EVIDENCE_MANIFEST_SHA256 = "603bb521d18d1b548000bf11b5dee68ef5b88f375162cd209bdb2d431776fcf9"


@dataclass(frozen=True)
class MigrationReceipt:
    """Attested filename and checksum for one pre-flatten migration."""

    version: int
    filename: str
    checksum: str


@dataclass(frozen=True)
class FlattenEvidence:
    """Pinned facts required before replacing historical receipts."""

    baseline_version: int
    baseline_checksum: str
    applied_versions: tuple[int, ...]
    receipts: tuple[MigrationReceipt, ...]


class MigrationFlattenError(RuntimeError):
    """Raised when flatten evidence or live bookkeeping is inconsistent."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise MigrationFlattenError(f"flatten evidence {label} must be an object")
    return value


def _verify_artifact(
    entry: object,
    *,
    expected_path: str,
    artifact_path: Path,
) -> str:
    artifact = _mapping(entry, expected_path)
    recorded_path = artifact.get("path")
    recorded_sha = artifact.get("sha256")
    if recorded_path != expected_path or not isinstance(recorded_sha, str):
        raise MigrationFlattenError(
            f"flatten evidence artifact metadata is invalid: {expected_path}"
        )
    observed_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if observed_sha != recorded_sha:
        raise MigrationFlattenError(f"flatten evidence artifact checksum mismatch: {expected_path}")
    return recorded_sha


def load_flatten_evidence(repo_root: Path | None = None) -> FlattenEvidence:
    """Load and verify the checked-in pre-flatten evidence bundle."""

    root = repo_root or Path(__file__).resolve().parents[3]
    evidence_dir = root / "docs/evidence/pre-flatten"
    manifest_path = evidence_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != _EVIDENCE_MANIFEST_SHA256:
        raise MigrationFlattenError("flatten evidence manifest checksum mismatch")
    manifest = _mapping(json.loads(manifest_bytes), "manifest")

    baseline_sha = _verify_artifact(
        manifest.get("baseline_schema"),
        expected_path="src/gobby/storage/postgres_baseline_schema.sql",
        artifact_path=root / "src/gobby/storage/postgres_baseline_schema.sql",
    )
    _verify_artifact(
        manifest.get("normalized_ddl"),
        expected_path="migrated-fresh.normalized.sql",
        artifact_path=evidence_dir / "migrated-fresh.normalized.sql",
    )
    _verify_artifact(
        manifest.get("seed_manifest"),
        expected_path="migrated-fresh.seed.json",
        artifact_path=evidence_dir / "migrated-fresh.seed.json",
    )
    _verify_artifact(
        manifest.get("divergence_ledger"),
        expected_path="docs/evidence/pre-flatten/divergence-ledger.md",
        artifact_path=evidence_dir / "divergence-ledger.md",
    )

    baseline_version = manifest.get("baseline_version")
    if baseline_version != BASELINE_VERSION or baseline_sha != baseline_checksum():
        raise MigrationFlattenError("flatten evidence does not match the installed baseline")

    applied_raw = manifest.get("applied_versions")
    receipts_raw = manifest.get("receipts")
    if not isinstance(applied_raw, list) or not all(isinstance(item, int) for item in applied_raw):
        raise MigrationFlattenError("flatten evidence applied_versions is invalid")
    if not isinstance(receipts_raw, list):
        raise MigrationFlattenError("flatten evidence receipts is invalid")

    receipts: list[MigrationReceipt] = []
    for index, raw_receipt in enumerate(receipts_raw):
        receipt = _mapping(raw_receipt, f"receipt {index}")
        version = receipt.get("version")
        filename = receipt.get("filename")
        checksum = receipt.get("checksum")
        if (
            not isinstance(version, int)
            or not isinstance(filename, str)
            or not isinstance(checksum, str)
        ):
            raise MigrationFlattenError(f"flatten evidence receipt {index} is invalid")
        receipts.append(MigrationReceipt(version, filename, checksum))

    evidence = FlattenEvidence(
        baseline_version=baseline_version,
        baseline_checksum=baseline_sha,
        applied_versions=tuple(applied_raw),
        receipts=tuple(receipts),
    )
    _expected_receipts(evidence)
    return evidence


def _no_fault(_point: str) -> None:
    return None


def _assert_no_foreign_connections(connection: psycopg.Connection[Any]) -> None:
    rows = connection.execute(
        """
        SELECT pid, application_name
        FROM pg_catalog.pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND backend_type = 'client backend'
        ORDER BY pid
        """
    ).fetchall()
    if rows:
        names = ", ".join(str(row["application_name"] or row["pid"]) for row in rows)
        raise MigrationFlattenError(f"foreign application connection blocks cutover: {names}")


def _expected_receipts(evidence: FlattenEvidence) -> dict[int, MigrationReceipt]:
    versions = evidence.applied_versions
    if tuple(sorted(set(versions))) != versions:
        raise MigrationFlattenError("flatten evidence applied versions must be sorted and unique")
    if not versions or versions[-1] != evidence.baseline_version:
        raise MigrationFlattenError(
            "flatten evidence baseline version must equal the pre-flatten schema head"
        )
    receipts = {receipt.version: receipt for receipt in evidence.receipts}
    expected_receipt_versions = {version for version in versions if version >= _BOOKKEEPING_VERSION}
    if set(receipts) != expected_receipt_versions:
        raise MigrationFlattenError("flatten evidence does not cover every receipted migration")
    return receipts


def _verify_pre_flatten_rows(
    rows: Sequence[Mapping[str, object]],
    evidence: FlattenEvidence,
) -> None:
    actual_versions = {_migration_version(row) for row in rows}
    if not set(evidence.applied_versions).issubset(actual_versions):
        raise MigrationFlattenError(
            "pre-flatten applied migration versions differ from pinned evidence"
        )
    expected_receipts = _expected_receipts(evidence)
    for row in rows:
        version = _migration_version(row)
        if version < _BOOKKEEPING_VERSION:
            if row["filename"] is not None or row["checksum"] is not None:
                raise MigrationFlattenError(
                    f"historical migration v{version} unexpectedly has receipt data"
                )
            continue
        expected = expected_receipts.get(version)
        if expected is None:
            raise MigrationFlattenError(
                "pre-flatten applied migration versions differ from pinned evidence"
            )
        if row["filename"] != expected.filename or row["checksum"] != expected.checksum:
            raise MigrationFlattenError(f"receipt mismatch for migration v{version}")


def _migration_version(row: Mapping[str, object]) -> int:
    version = row["version"]
    if not isinstance(version, int):
        raise MigrationFlattenError("migration version must be an integer")
    return version


def cutover_migration_bookkeeping(
    database_url: str,
    epoch_id: uuid.UUID | str,
    evidence: FlattenEvidence,
    *,
    fault_hook: Callable[[str], None] = _no_fault,
) -> None:
    """Replace historical rows with one baseline receipt under epoch and lock."""

    _expected_receipts(evidence)
    epoch = require_orchestrator_epoch(database_url, epoch_id, campaign="flatten")
    bound_url = bind_maintenance_epoch(database_url, epoch.id)
    with psycopg.connect(
        bound_url,
        autocommit=True,
        application_name="gobby-flatten-cutover",
        row_factory=dict_row,
    ) as connection:
        connection.execute(f"SELECT pg_advisory_lock({MIGRATION_LOCK_SQL})")
        try:
            _assert_no_foreign_connections(connection)
            with connection.transaction():
                rows = connection.execute(
                    """
                    SELECT version, filename, checksum
                    FROM schema_migrations
                    ORDER BY version
                    """
                ).fetchall()
                _verify_pre_flatten_rows(rows, evidence)
                connection.execute("DELETE FROM schema_migrations")
                fault_hook("after_delete")
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, filename, checksum, applied_at)
                    VALUES (%s, %s, %s, NOW())
                    """,
                    (
                        evidence.baseline_version,
                        f"baseline@{evidence.baseline_version}",
                        evidence.baseline_checksum,
                    ),
                )
        finally:
            connection.execute(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_SQL})")


def verify_flattened_bookkeeping(
    database_url: str,
    epoch_id: uuid.UUID | str,
    evidence: FlattenEvidence,
) -> None:
    """Require the exact post-cutover singleton baseline receipt."""

    epoch = require_orchestrator_epoch(database_url, epoch_id, campaign="flatten")
    with psycopg.connect(
        bind_maintenance_epoch(database_url, epoch.id),
        autocommit=True,
        application_name="gobby-flatten-verify",
    ) as connection:
        rows = connection.execute(
            "SELECT version, filename, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    expected = [
        (
            evidence.baseline_version,
            f"baseline@{evidence.baseline_version}",
            evidence.baseline_checksum,
        )
    ]
    if rows != expected:
        raise MigrationFlattenError("flattened migration bookkeeping receipt is invalid")
