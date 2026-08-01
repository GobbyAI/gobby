"""Schema migration commands and destructive-apply authorization."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import click

from gobby.cli.hub_backup._manifest import (
    DEFAULT_MAX_AGE_HOURS,
    MANIFEST_NAME,
    HubBackupManifest,
    SourceIdentity,
    check_manifest_gate,
    load_manifest,
)
from gobby.cli.hub_backup._stores import collect_postgres_identity
from gobby.cli.hub_maintenance import CampaignExecutor, register_campaign_executor
from gobby.config.app import load_config
from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.paths import get_gobby_home
from gobby.storage.hub.runtime import apply_destructive_batch
from gobby.storage.maintenance_epoch import (
    DestructiveBatch,
    MaintenanceEpoch,
    bind_maintenance_epoch,
    discover_active_maintenance_epoch,
    get_destructive_batch,
    require_orchestrator_epoch,
)
from gobby.storage.migrations import (
    DestructiveMigrationContext,
    latest_known_version,
)


class SchemaGateError(RuntimeError):
    """Raised when backup or maintenance evidence cannot authorize schema mutation."""


def validate_destructive_manifest(
    manifest: HubBackupManifest,
    *,
    manifest_sha256: str,
    current_identity: SourceIdentity,
    epoch: MaintenanceEpoch,
    batch: DestructiveBatch,
    now: datetime,
    max_age_hours: float,
) -> DestructiveMigrationContext:
    """Validate every external fact required before a destructive batch."""
    decision = check_manifest_gate(
        manifest,
        current_identity=current_identity,
        now=now,
        max_age_hours=max_age_hours,
    )
    reasons = list(decision.reasons)
    if manifest.epoch_id != str(epoch.id):
        reasons.append(f"manifest epoch {manifest.epoch_id!r} does not match open epoch {epoch.id}")
    if batch.maintenance_epoch_id != epoch.id:
        reasons.append("destructive batch does not belong to the open epoch")
    if batch.backup_manifest_sha256 != manifest_sha256:
        reasons.append("backup manifest digest does not match destructive batch")
    if epoch.released_at is not None:
        reasons.append("maintenance epoch is not open")
    if epoch.campaign != "schema-apply" or batch.campaign != "schema-apply":
        reasons.append("maintenance epoch and batch must use the schema-apply campaign")
    if epoch.opened_by != "hub-maintenance:schema-apply":
        reasons.append("maintenance epoch is not owned by hub-maintenance:schema-apply")
    if reasons:
        raise SchemaGateError("; ".join(reasons))
    return DestructiveMigrationContext(
        epoch_id=str(epoch.id),
        batch_id=str(batch.id),
        manifest_sha256=manifest_sha256,
        backup_starting_head=manifest.backup_starting_head,
    )


@click.group("schema")
def schema() -> None:
    """Inspect or apply the hub schema migration chain."""


@schema.command("apply")
@click.option(
    "--destructive",
    is_flag=True,
    help="Apply the epoch-bound destructive batch after verifying backup evidence.",
)
@click.option(
    "--max-age-hours",
    type=click.FloatRange(min=0.0, min_open=True),
    default=DEFAULT_MAX_AGE_HOURS,
    show_default=True,
    help="Maximum accepted age of the restore-verified backup manifest.",
)
@click.pass_context
def apply_schema(
    ctx: click.Context,
    *,
    destructive: bool,
    max_age_hours: float,
) -> None:
    """Apply pending migrations, stopping at destructive work by default."""
    if not destructive:
        from gobby.cli.runtime import get_cli_runtime

        get_cli_runtime(ctx).require_database()
        click.echo(f"Schema is at version {latest_known_version()}")
        return

    config_file = ctx.find_root().params.get("config")
    config = load_config(
        config_file if isinstance(config_file, str) else None,
        resolve_database_url=True,
    )
    if config.database_url is None:
        raise click.ClickException("Hub database URL is unavailable")
    epoch = discover_active_maintenance_epoch(config.database_url)
    if epoch is None:
        raise click.ClickException(
            "No maintenance epoch is open; run `gobby hub-maintenance run schema-apply`."
        )
    try:
        epoch = require_orchestrator_epoch(
            config.database_url,
            epoch.id,
            campaign="schema-apply",
        )
        batch = get_destructive_batch(config.database_url, epoch.id)
        if batch is None:
            raise SchemaGateError("Open maintenance epoch has no destructive batch intent")
        _apply_verified_batch(
            config.database_url,
            config.postgres_pool,
            epoch,
            batch,
            max_age_hours=max_age_hours,
        )
    except (OSError, ValueError, SchemaGateError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Schema destructive batch {batch.id} applied")


def _apply_verified_batch(
    database_url: str,
    pool_config: PostgresPoolConfig,
    epoch: MaintenanceEpoch,
    batch: DestructiveBatch,
    *,
    max_age_hours: float,
) -> None:
    manifest_path = _newest_manifest_path(get_gobby_home() / "backups" / "hub")
    if batch.backup_manifest_path is None:
        raise SchemaGateError("Destructive batch has no backup manifest path")
    if manifest_path.resolve() != Path(batch.backup_manifest_path).resolve():
        raise SchemaGateError(
            "Newest backup manifest is not the epoch-bound destructive batch manifest"
        )
    manifest = load_manifest(manifest_path)
    manifest_sha256 = _file_sha256(manifest_path)
    bound_url = bind_maintenance_epoch(database_url, epoch.id)
    current_identity, _current_head = collect_postgres_identity(bound_url)
    context = validate_destructive_manifest(
        manifest,
        manifest_sha256=manifest_sha256,
        current_identity=current_identity,
        epoch=epoch,
        batch=batch,
        now=datetime.now(UTC),
        max_age_hours=max_age_hours,
    )
    apply_destructive_batch(bound_url, pool_config, context)


def _newest_manifest_path(backup_root: Path) -> Path:
    candidates = list(backup_root.glob(f"*/{MANIFEST_NAME}"))
    if not candidates:
        raise SchemaGateError(f"No hub backup manifests found under {backup_root}")
    manifests = [(load_manifest(path), path) for path in candidates]
    return max(
        manifests,
        key=lambda item: datetime.fromisoformat(item[0].created_at),
    )[1]


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class _SchemaApplyExecutor(CampaignExecutor):
    def apply(self, epoch: MaintenanceEpoch, batch: DestructiveBatch) -> None:
        config = load_config(resolve_database_url=True)
        if config.database_url is None:
            raise SchemaGateError("Hub database URL is unavailable")
        _apply_verified_batch(
            config.database_url,
            config.postgres_pool,
            epoch,
            batch,
            max_age_hours=DEFAULT_MAX_AGE_HOURS,
        )

    def verify(self, epoch: MaintenanceEpoch, batch: DestructiveBatch) -> None:
        config = load_config(resolve_database_url=True)
        if config.database_url is None:
            raise SchemaGateError("Hub database URL is unavailable")
        _identity, current_head = collect_postgres_identity(
            bind_maintenance_epoch(config.database_url, epoch.id)
        )
        if current_head != latest_known_version():
            raise SchemaGateError(
                f"Schema batch stopped at version {current_head}; expected {latest_known_version()}"
            )


register_campaign_executor("schema-apply", _SchemaApplyExecutor())
