from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from gobby.cli import cli as root_cli
from gobby.cli.hub_backup._manifest import (
    HubBackupManifest,
    SourceIdentity,
    StoreRecord,
    VerificationState,
)
from gobby.cli.schema import SchemaGateError, validate_destructive_manifest
from gobby.storage.maintenance_epoch import Campaign, DestructiveBatch, MaintenanceEpoch

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 31, 12, tzinfo=UTC)
IDENTITY = SourceIdentity(
    pg_system_identifier="7642479605904478251",
    database_name="gobby",
    database_oid=16384,
)


def _verified_state() -> VerificationState:
    return VerificationState(
        verified=True,
        method="scratch restore",
        timestamp=NOW.isoformat(),
    )


def _manifest(epoch_id: uuid.UUID) -> HubBackupManifest:
    return HubBackupManifest(
        created_at=(NOW - timedelta(hours=1)).isoformat(),
        gobby_version="0.4.98",
        epoch_id=str(epoch_id),
        source_identity=IDENTITY,
        backup_starting_head=354,
        row_count_probes={},
        artifacts=[],
        stores={
            store: StoreRecord(
                archive_verified=_verified_state(),
                restore_verified=_verified_state(),
                details={},
            )
            for store in ("postgres", "qdrant", "falkordb", "volumes")
        },
    )


def _epoch(epoch_id: uuid.UUID, campaign: Campaign = "schema-apply") -> MaintenanceEpoch:
    return MaintenanceEpoch(
        id=epoch_id,
        campaign=campaign,
        opened_at=NOW,
        opened_by=f"hub-maintenance:{campaign}",
        scope_note="schema apply",
        released_at=None,
        released_by_command=None,
    )


def _batch(
    epoch_id: uuid.UUID, digest: str, campaign: Campaign = "schema-apply"
) -> DestructiveBatch:
    return DestructiveBatch(
        id=uuid.uuid4(),
        maintenance_epoch_id=epoch_id,
        campaign=campaign,
        status="pending",
        backup_manifest_path="/tmp/manifest.json",
        backup_manifest_sha256=digest,
        intent={"campaign": campaign},
        migration_plan=[],
        target_receipts={},
        created_at=NOW,
        updated_at=NOW,
        verified_at=None,
        aborted_at=None,
        abort_disposition=None,
    )


def test_destructive_manifest_gate_accepts_all_bound_evidence(tmp_path: Path) -> None:
    epoch_id = uuid.uuid4()
    digest = "a" * 64

    validator = cast(Callable[..., object], validate_destructive_manifest)
    assert (
        validator(
            _manifest(epoch_id),
            backup_root=tmp_path,
            manifest_sha256=digest,
            current_identity=IDENTITY,
            epoch=_epoch(epoch_id),
            batch=_batch(epoch_id, digest),
            now=NOW,
            max_age_hours=24,
        )
        is None
    )


def test_destructive_manifest_gate_accepts_identity_cutover_campaign(tmp_path: Path) -> None:
    epoch_id = uuid.uuid4()
    digest = "a" * 64

    validator = cast(Callable[..., object], validate_destructive_manifest)
    assert (
        validator(
            _manifest(epoch_id),
            backup_root=tmp_path,
            manifest_sha256=digest,
            current_identity=IDENTITY,
            epoch=_epoch(epoch_id, campaign="identity-cutover"),
            batch=_batch(epoch_id, digest, campaign="identity-cutover"),
            now=NOW,
            max_age_hours=24,
        )
        is None
    )


def test_destructive_manifest_gate_refuses_epoch_batch_campaign_mismatch(
    tmp_path: Path,
) -> None:
    epoch_id = uuid.uuid4()
    digest = "a" * 64

    with pytest.raises(SchemaGateError, match="campaigns do not match"):
        validate_destructive_manifest(
            _manifest(epoch_id),
            backup_root=tmp_path,
            manifest_sha256=digest,
            current_identity=IDENTITY,
            epoch=_epoch(epoch_id, campaign="identity-cutover"),
            batch=_batch(epoch_id, digest),
            now=NOW,
            max_age_hours=24,
        )


def test_destructive_manifest_gate_refuses_foreign_epoch_owner(tmp_path: Path) -> None:
    epoch_id = uuid.uuid4()
    digest = "a" * 64
    epoch = replace(
        _epoch(epoch_id, campaign="identity-cutover"),
        opened_by="hub-maintenance:schema-apply",
    )

    with pytest.raises(SchemaGateError, match="not owned by hub-maintenance:identity-cutover"):
        validate_destructive_manifest(
            _manifest(epoch_id),
            backup_root=tmp_path,
            manifest_sha256=digest,
            current_identity=IDENTITY,
            epoch=epoch,
            batch=_batch(epoch_id, digest, campaign="identity-cutover"),
            now=NOW,
            max_age_hours=24,
        )


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("restore", "restore_verified"),
        ("freshness", "max age"),
        ("identity", "identity fingerprint mismatch"),
        ("epoch", "epoch"),
        ("digest", "digest"),
    ],
)
def test_destructive_manifest_gate_refuses_each_failure_mode(
    failure: str,
    message: str,
    tmp_path: Path,
) -> None:
    epoch_id = uuid.uuid4()
    digest = "a" * 64
    manifest = _manifest(epoch_id)
    epoch = _epoch(epoch_id)
    batch = _batch(epoch_id, digest)
    identity = IDENTITY

    if failure == "restore":
        manifest.stores["postgres"] = replace(
            manifest.stores["postgres"],
            restore_verified=VerificationState(False, None, None),
        )
    elif failure == "freshness":
        manifest.created_at = (NOW - timedelta(hours=25)).isoformat()
    elif failure == "identity":
        identity = replace(IDENTITY, database_oid=999)
    elif failure == "epoch":
        manifest.epoch_id = str(uuid.uuid4())
    elif failure == "digest":
        batch = replace(batch, backup_manifest_sha256="b" * 64)

    with pytest.raises(SchemaGateError, match=message):
        validate_destructive_manifest(
            manifest,
            backup_root=tmp_path,
            manifest_sha256=digest,
            current_identity=identity,
            epoch=epoch,
            batch=batch,
            now=NOW,
            max_age_hours=24,
        )


def test_schema_command_is_registered_on_root_cli() -> None:
    assert "schema" in root_cli.commands
