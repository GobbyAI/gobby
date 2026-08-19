from __future__ import annotations

import importlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from click.testing import CliRunner

from gobby.cli import cli as root_cli
from gobby.cli.hub_backup._manifest import (
    HubBackupManifest,
    SourceIdentity,
    StoreRecord,
    VerificationState,
)
from gobby.cli.schema import SchemaGateError, validate_destructive_manifest
from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.storage.maintenance_epoch import Campaign, DestructiveBatch, MaintenanceEpoch

pytestmark = pytest.mark.unit
schema_module = importlib.import_module("gobby.cli.schema")

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
            for store in ("postgres", "qdrant", "falkordb", "volumes", "files")
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


def test_destructive_manifest_gate_accepts_reconcile_campaign(tmp_path: Path) -> None:
    epoch_id = uuid.uuid4()
    digest = "a" * 64

    validator = cast(Callable[..., object], validate_destructive_manifest)
    assert (
        validator(
            _manifest(epoch_id),
            backup_root=tmp_path,
            manifest_sha256=digest,
            current_identity=IDENTITY,
            epoch=_epoch(epoch_id, campaign="reconcile"),
            batch=_batch(epoch_id, digest, campaign="reconcile"),
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
            epoch=_epoch(epoch_id, campaign="reconcile"),
            batch=_batch(epoch_id, digest),
            now=NOW,
            max_age_hours=24,
        )


def test_destructive_manifest_gate_refuses_foreign_epoch_owner(tmp_path: Path) -> None:
    epoch_id = uuid.uuid4()
    digest = "a" * 64
    epoch = replace(
        _epoch(epoch_id, campaign="reconcile"),
        opened_by="hub-maintenance:schema-apply",
    )

    with pytest.raises(SchemaGateError, match="not owned by hub-maintenance:reconcile"):
        validate_destructive_manifest(
            _manifest(epoch_id),
            backup_root=tmp_path,
            manifest_sha256=digest,
            current_identity=IDENTITY,
            epoch=epoch,
            batch=_batch(epoch_id, digest, campaign="reconcile"),
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


def test_destructive_manifest_gate_refuses_missing_files_store(tmp_path: Path) -> None:
    epoch_id = uuid.uuid4()
    manifest = _manifest(epoch_id)
    manifest.stores.pop("files")
    with pytest.raises(SchemaGateError, match="files"):
        validate_destructive_manifest(
            manifest,
            backup_root=tmp_path,
            manifest_sha256="a" * 64,
            current_identity=IDENTITY,
            epoch=_epoch(epoch_id),
            batch=_batch(epoch_id, "a" * 64),
            now=NOW,
            max_age_hours=24,
        )


def test_schema_command_is_registered_on_root_cli() -> None:
    assert "schema" in root_cli.commands


def test_hub_bootstrap_resolves_dsn_from_bootstrap_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_module = importlib.import_module("gobby.cli.schema")
    from gobby.config.bootstrap import BootstrapConfig

    captured: dict[str, bool] = {}

    def fake_load(*, resolve_database_url: bool) -> BootstrapConfig:
        captured["resolve_database_url"] = resolve_database_url
        return BootstrapConfig(database_url="postgresql://gobby:pw@127.0.0.1:60892/hub")

    monkeypatch.setattr(schema_module, "load_bootstrap", fake_load)

    database_url, pool_config = schema_module._hub_bootstrap()

    assert database_url == "postgresql://gobby:pw@127.0.0.1:60892/hub"
    assert pool_config == BootstrapConfig().postgres_pool
    assert captured["resolve_database_url"] is True


def test_hub_bootstrap_refuses_missing_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_module = importlib.import_module("gobby.cli.schema")
    from gobby.config.bootstrap import BootstrapConfig

    monkeypatch.setattr(
        schema_module,
        "load_bootstrap",
        lambda *, resolve_database_url: BootstrapConfig(database_url=None),
    )

    with pytest.raises(SchemaGateError, match="Hub database URL is unavailable"):
        schema_module._hub_bootstrap()


def test_schema_apply_executor_verify_uses_bootstrap_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    schema_module = importlib.import_module("gobby.cli.schema")

    monkeypatch.setattr(
        schema_module,
        "_hub_bootstrap",
        lambda: ("postgresql://gobby:pw@127.0.0.1:60892/hub", MagicMock()),
    )
    bound = MagicMock()
    bind = MagicMock(return_value=bound)
    collect = MagicMock(return_value=(MagicMock(), 42))
    monkeypatch.setattr(schema_module, "bind_maintenance_epoch", bind)
    monkeypatch.setattr(schema_module, "collect_postgres_identity", collect)
    monkeypatch.setattr(schema_module, "latest_schema_version", lambda: 42)
    epoch = MagicMock(id=uuid.uuid4())

    schema_module._SchemaApplyExecutor().verify(epoch, MagicMock())

    assert bind.call_count == 1
    assert bind.call_args.args == ("postgresql://gobby:pw@127.0.0.1:60892/hub", epoch.id)
    assert collect.call_count == 1
    assert collect.call_args.args == (bound,)


def test_destructive_schema_apply_composes_epoch_manifest_and_execution_guards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = "postgresql://gobby:pw@127.0.0.1:60892/hub"
    bound_url = f"{database_url}?options=epoch"
    epoch = _epoch(uuid.uuid4())
    digest = "a" * 64
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    batch = replace(
        _batch(epoch.id, digest),
        backup_manifest_path=str(manifest_path),
    )
    events: list[str] = []

    def discover(dsn: str) -> MaintenanceEpoch:
        assert dsn == database_url
        events.append("discover")
        return epoch

    def require(dsn: str, epoch_id: uuid.UUID, *, campaign: Campaign) -> MaintenanceEpoch:
        assert (dsn, epoch_id, campaign) == (database_url, epoch.id, "schema-apply")
        events.append("require")
        return epoch

    def get_batch(dsn: str, epoch_id: uuid.UUID) -> DestructiveBatch:
        assert (dsn, epoch_id) == (database_url, epoch.id)
        events.append("batch")
        return batch

    real_validate = schema_module.validate_destructive_manifest

    def validate(*args: object, **kwargs: object) -> None:
        events.append("validate")
        cast(Callable[..., None], real_validate)(*args, **kwargs)

    def apply(dsn: str, pool_config: PostgresPoolConfig) -> None:
        assert dsn == bound_url
        assert isinstance(pool_config, PostgresPoolConfig)
        assert events[-1] == "validate"
        events.append("apply")

    monkeypatch.setattr(
        schema_module,
        "_hub_bootstrap",
        lambda: (database_url, PostgresPoolConfig()),
    )
    monkeypatch.setattr(schema_module, "discover_active_maintenance_epoch", discover)
    monkeypatch.setattr(schema_module, "require_orchestrator_epoch", require)
    monkeypatch.setattr(schema_module, "get_destructive_batch", get_batch)
    monkeypatch.setattr(schema_module, "_newest_manifest_path", lambda _root: manifest_path)
    monkeypatch.setattr(schema_module, "load_manifest", lambda _path: _manifest(epoch.id))
    monkeypatch.setattr(schema_module, "_file_sha256", lambda _path: digest)
    monkeypatch.setattr(schema_module, "bind_maintenance_epoch", lambda *_args: bound_url)
    monkeypatch.setattr(
        schema_module,
        "collect_postgres_identity",
        lambda _dsn: (IDENTITY, 42),
    )
    monkeypatch.setattr(schema_module, "validate_destructive_manifest", validate)
    monkeypatch.setattr(schema_module, "apply_destructive_batch", apply)

    result = CliRunner().invoke(
        root_cli,
        ["schema", "apply", "--destructive", "--max-age-hours", "10000"],
    )

    assert result.exit_code == 0, result.output
    assert str(batch.id) in result.output
    assert events == ["discover", "require", "batch", "validate", "apply"]


def test_destructive_schema_apply_without_epoch_aborts_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql://gobby:pw@127.0.0.1:60892/hub"
    applied = False

    def apply(_dsn: str, _pool_config: PostgresPoolConfig) -> None:
        nonlocal applied
        applied = True

    monkeypatch.setattr(
        schema_module,
        "_hub_bootstrap",
        lambda: (database_url, PostgresPoolConfig()),
    )
    monkeypatch.setattr(
        schema_module,
        "discover_active_maintenance_epoch",
        lambda _dsn: None,
    )
    monkeypatch.setattr(schema_module, "apply_destructive_batch", apply)

    result = CliRunner().invoke(root_cli, ["schema", "apply", "--destructive"])

    assert result.exit_code == 1
    assert "No maintenance epoch is open" in result.output
    assert applied is False
