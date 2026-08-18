"""gobby-hub-backup-manifest schema v3: the verified-restore backup contract.

This manifest is a named, versioned cross-language contract. The Python
producer here and gcore's Rust gated-apply reader both validate against
this JSON schema, so field changes require a version bump.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema

from gobby.cli.hub_backup._integrity import (
    artifact_integrity_errors,
    open_exclusive_binary,
    read_bytes_no_follow,
)

MANIFEST_FORMAT = "gobby-hub-backup-manifest"
MANIFEST_VERSION = 3
MANIFEST_NAME = "manifest.json"
DEFAULT_MAX_AGE_HOURS = 24.0

STORE_KEYS = ("postgres", "qdrant", "falkordb", "volumes", "files")

_SHA256_PATTERN = "^[0-9a-f]{64}$"

_VERIFICATION_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verified", "method", "timestamp"],
    "properties": {
        "verified": {"type": "boolean"},
        "method": {"type": ["string", "null"]},
        "timestamp": {"type": ["string", "null"]},
    },
}

HUB_BACKUP_MANIFEST_SCHEMA_V3: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://gobby.ai/schemas/hub-backup-manifest.v3.schema.json",
    "title": "gobby-hub-backup-manifest v3",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "manifest_format",
        "manifest_version",
        "created_at",
        "gobby_version",
        "epoch_id",
        "source_identity",
        "backup_starting_head",
        "row_count_probes",
        "artifacts",
        "stores",
    ],
    "properties": {
        "manifest_format": {"const": MANIFEST_FORMAT},
        "manifest_version": {"const": MANIFEST_VERSION},
        "created_at": {"type": "string"},
        "gobby_version": {"type": "string"},
        "epoch_id": {"type": ["string", "null"]},
        "source_identity": {
            "type": "object",
            "additionalProperties": False,
            "required": ["pg_system_identifier", "database_name", "database_oid"],
            "properties": {
                "pg_system_identifier": {"type": "string"},
                "database_name": {"type": "string"},
                "database_oid": {"type": "integer"},
            },
        },
        "backup_starting_head": {"type": "integer"},
        "row_count_probes": {
            "type": "object",
            "additionalProperties": {"type": "integer"},
        },
        "artifacts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "path", "sha256", "size_bytes"],
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "sha256": {"type": "string", "pattern": _SHA256_PATTERN},
                    "size_bytes": {"type": "integer", "minimum": 0},
                },
            },
        },
        "stores": {
            "type": "object",
            "additionalProperties": False,
            "required": list(STORE_KEYS),
            "properties": {
                key: {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["archive_verified", "restore_verified", "details"],
                    "properties": {
                        "archive_verified": _VERIFICATION_STATE_SCHEMA,
                        "restore_verified": _VERIFICATION_STATE_SCHEMA,
                        "details": {"type": "object"},
                    },
                }
                for key in STORE_KEYS
            },
        },
    },
}


@dataclass(frozen=True)
class VerificationState:
    """One verification outcome: what was proven, how, and when."""

    verified: bool
    method: str | None
    timestamp: str | None


@dataclass(frozen=True)
class ArtifactRecord:
    """One backup artifact with its integrity fingerprint."""

    name: str
    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class SourceIdentity:
    """Stable source-database identity.

    The pg_control system identifier plus database name plus pg_database
    oid — a same-name drop/recreate changes the oid, so name alone can
    false-match.
    """

    pg_system_identifier: str
    database_name: str
    database_oid: int


@dataclass
class StoreRecord:
    """Per-store verification states and store-specific facts."""

    archive_verified: VerificationState
    restore_verified: VerificationState
    details: dict[str, Any]


@dataclass
class HubBackupManifest:
    """The v3 hub backup manifest."""

    created_at: str
    gobby_version: str
    epoch_id: str | None
    source_identity: SourceIdentity
    backup_starting_head: int
    row_count_probes: dict[str, int]
    artifacts: list[ArtifactRecord]
    stores: dict[str, StoreRecord]
    manifest_format: str = MANIFEST_FORMAT
    manifest_version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_format": self.manifest_format,
            "manifest_version": self.manifest_version,
            "created_at": self.created_at,
            "gobby_version": self.gobby_version,
            "epoch_id": self.epoch_id,
            "source_identity": asdict(self.source_identity),
            "backup_starting_head": self.backup_starting_head,
            "row_count_probes": dict(self.row_count_probes),
            "artifacts": [asdict(artifact) for artifact in self.artifacts],
            "stores": {key: asdict(store) for key, store in self.stores.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HubBackupManifest:
        validate_manifest_data(data)
        return cls(
            created_at=data["created_at"],
            gobby_version=data["gobby_version"],
            epoch_id=data["epoch_id"],
            source_identity=SourceIdentity(**data["source_identity"]),
            backup_starting_head=data["backup_starting_head"],
            row_count_probes=dict(data["row_count_probes"]),
            artifacts=[ArtifactRecord(**artifact) for artifact in data["artifacts"]],
            stores={
                key: StoreRecord(
                    archive_verified=VerificationState(**store["archive_verified"]),
                    restore_verified=VerificationState(**store["restore_verified"]),
                    details=dict(store["details"]),
                )
                for key, store in data["stores"].items()
            },
        )


@dataclass(frozen=True)
class GateDecision:
    """Outcome of the destructive-gate manifest check."""

    ok: bool
    reasons: list[str]


def validate_manifest_data(data: dict[str, Any]) -> None:
    """Validate raw manifest data against the v3 JSON schema."""
    try:
        jsonschema.validate(data, HUB_BACKUP_MANIFEST_SCHEMA_V3)
    except jsonschema.ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "$"
        raise ValueError(
            f"manifest does not satisfy {MANIFEST_FORMAT} v{MANIFEST_VERSION} "
            f"schema at {location}: {exc.message}"
        ) from exc


def write_manifest(manifest: HubBackupManifest, path: Path) -> None:
    """Validate and write the manifest with owner-only permissions."""
    data = manifest.to_dict()
    validate_manifest_data(data)
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    with open_exclusive_binary(path, label="backup manifest") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def load_manifest(path: Path) -> HubBackupManifest:
    """Load and schema-validate a manifest file."""
    try:
        data = json.loads(read_bytes_no_follow(path, label="backup manifest").decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"manifest is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"manifest must be a JSON object: {path}")
    return HubBackupManifest.from_dict(data)


def check_manifest_gate(
    manifest: HubBackupManifest,
    *,
    backup_root: Path,
    current_identity: SourceIdentity,
    now: datetime,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> GateDecision:
    """Decide whether a manifest authorizes destructive work.

    Refuses stale or altered artifacts, an old manifest, unverified stores,
    or a source identity that does not fingerprint-match the target database.
    """
    reasons: list[str] = []
    reasons.extend(artifact_integrity_errors(backup_root, manifest.artifacts))

    created_at = datetime.fromisoformat(manifest.created_at)
    age = now - created_at
    limit = timedelta(hours=max_age_hours)
    if age > limit:
        reasons.append(
            f"manifest exceeds max age: created {manifest.created_at}, age {age} > {max_age_hours}h"
        )

    unverified = sorted(
        key for key, store in manifest.stores.items() if not store.restore_verified.verified
    )
    if unverified:
        reasons.append("restore_verified not earned for stores: " + ", ".join(unverified))
    files = manifest.stores.get("files")
    if files is None:
        reasons.append("required store missing: files")
    elif not files.archive_verified.verified or not files.restore_verified.verified:
        reasons.append("files store is not archive_verified and restore_verified")

    if manifest.source_identity != current_identity:
        reasons.append(
            "source identity fingerprint mismatch: manifest "
            f"{asdict(manifest.source_identity)} != target {asdict(current_identity)}"
        )

    return GateDecision(ok=not reasons, reasons=reasons)
