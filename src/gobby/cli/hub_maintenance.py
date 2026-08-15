"""Hub-wide maintenance epoch orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess  # nosec B404 - fixed Python module invocation, never shell=True
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import click
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

from gobby.cli.hub_backup.cli import _start_daemon
from gobby.cli.postgres_backup import _resolve_database_url as _resolve_backup_database_url
from gobby.cli.utils_shutdown import stop_daemon
from gobby.paths import get_gobby_home
from gobby.storage.account_identity_cutover import ACCOUNT_IDENTITY_CAMPAIGN
from gobby.storage.maintenance_epoch import (
    CAMPAIGNS,
    Campaign,
    DestructiveBatch,
    JsonObject,
    MaintenanceEpoch,
    abort_maintenance_epoch,
    create_destructive_batch,
    discover_active_maintenance_epoch,
    get_destructive_batch,
    maintenance_child_environment,
    mark_batch_applied,
    mark_batch_verified,
    open_maintenance_epoch,
    record_batch_backup,
    release_maintenance_epoch,
)
from gobby.utils.env import is_test_protect_enabled


class CampaignExecutor(Protocol):
    """Campaign-specific mutation and verification inside a shared epoch."""

    def apply(self, epoch: MaintenanceEpoch, batch: DestructiveBatch) -> None:
        """Apply or idempotently resume the campaign mutation."""
        ...

    def verify(self, epoch: MaintenanceEpoch, batch: DestructiveBatch) -> None:
        """Prove the campaign's exact postconditions."""
        ...


@dataclass(frozen=True)
class BackupEvidence:
    """Epoch-bound backup manifest identity."""

    manifest_path: Path
    manifest_sha256: str


_CAMPAIGN_EXECUTORS: dict[Campaign, CampaignExecutor] = {}


def register_campaign_executor(
    campaign: Campaign,
    executor: CampaignExecutor,
) -> None:
    """Register the implementation owned by a dependent campaign task."""
    if campaign not in CAMPAIGNS:
        raise ValueError(f"Unknown maintenance campaign: {campaign}")
    _CAMPAIGN_EXECUTORS[campaign] = executor


@click.group("hub-maintenance")
def hub_maintenance() -> None:
    """Fence the hub while a verified destructive campaign runs."""


@hub_maintenance.command("run")
@click.argument("campaign", type=click.Choice(CAMPAIGNS))
@click.pass_context
def run_campaign(ctx: click.Context, campaign: Campaign) -> None:
    """Open a new epoch and run CAMPAIGN through verified release."""
    executor = _load_campaign_executor(campaign)
    intent: JsonObject = {"campaign": campaign}
    database_url = _resolve_database_url()
    _stop_daemon_before_fence(database_url)
    owner_command = f"hub-maintenance:{campaign}"
    epoch = open_maintenance_epoch(
        database_url,
        campaign=campaign,
        opened_by=owner_command,
        scope_note=f"{campaign} campaign",
    )
    batch = create_destructive_batch(
        database_url,
        epoch.id,
        campaign=campaign,
        intent=intent,
    )
    _finish_or_report(
        ctx,
        database_url=database_url,
        epoch=epoch,
        batch=batch,
        executor=executor,
        released_by_command=f"hub-maintenance run {campaign}",
    )
    click.echo(f"Maintenance campaign {campaign} completed in epoch {epoch.id}")


@hub_maintenance.command("resume")
@click.pass_context
def resume_campaign(ctx: click.Context) -> None:
    """Resume the open campaign using only hub-resident state."""
    database_url = _resolve_database_url()
    _stop_daemon_before_fence(database_url)
    epoch = discover_active_maintenance_epoch(database_url)
    if epoch is None:
        raise click.ClickException("No maintenance epoch is open")
    batch = get_destructive_batch(database_url, epoch.id)
    if batch is None:
        raise click.ClickException(f"Maintenance epoch {epoch.id} has no destructive batch intent")
    executor = _load_campaign_executor(epoch.campaign)
    _finish_or_report(
        ctx,
        database_url=database_url,
        epoch=epoch,
        batch=batch,
        executor=executor,
        released_by_command=f"hub-maintenance resume {epoch.campaign}",
    )
    click.echo(f"Maintenance campaign {epoch.campaign} resumed and completed")


@hub_maintenance.command("status")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def maintenance_status(json_output: bool) -> None:
    """Report the open epoch and its destructive batch."""
    database_url = _resolve_database_url()
    epoch = discover_active_maintenance_epoch(database_url)
    batch = get_destructive_batch(database_url, epoch.id) if epoch is not None else None
    payload = {
        "epoch": _epoch_payload(epoch) if epoch is not None else None,
        "batch": _batch_payload(batch) if batch is not None else None,
    }
    if json_output:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if epoch is None:
        click.echo("No maintenance epoch is open")
        return
    click.echo(f"Epoch:    {epoch.id}")
    click.echo(f"Campaign: {epoch.campaign}")
    click.echo(f"Opened:   {epoch.opened_at.isoformat()}")
    click.echo(f"Batch:    {batch.status if batch is not None else 'missing'}")


@hub_maintenance.command("abort")
@click.option(
    "--disposition",
    required=True,
    help="Recorded evidence describing the verified partial-state disposition.",
)
def abort_campaign(disposition: str) -> None:
    """Explicitly release an epoch after recording partial-state evidence."""
    database_url = _resolve_database_url()
    epoch = discover_active_maintenance_epoch(database_url)
    if epoch is None:
        raise click.ClickException("No maintenance epoch is open")
    batch = get_destructive_batch(database_url, epoch.id)
    state = batch.status if batch is not None else "missing"
    click.confirm(
        f"Abort epoch {epoch.id} ({epoch.campaign}, batch {state}) and release the fence?",
        abort=True,
    )
    abort_maintenance_epoch(
        database_url,
        epoch.id,
        disposition=disposition,
        confirmed=True,
    )
    _start_daemon()
    click.echo(f"Maintenance epoch {epoch.id} aborted and released")


def _stop_daemon_before_fence(database_url: str) -> None:
    """Stop the live daemon before the login fence can strand its pool.

    An epoch opened against a running daemon terminates its pool connections
    and then rejects every reconnect, wedging the daemon until restart
    (#19437). The fence must only ever go up over a quiesced hub.
    """
    if is_test_protect_enabled():
        try:
            database_name = conninfo_to_dict(database_url).get("dbname")
        except ProgrammingError:
            database_name = None
        if database_name != "gobby_test":
            raise click.ClickException(
                "GOBBY_TEST_PROTECT permits hub maintenance only against the exact "
                "database name 'gobby_test'; the configured target is missing, malformed, "
                "or different"
            )
    if not stop_daemon(shutdown_source="cli_hub_maintenance"):
        raise click.ClickException(
            "Could not stop the running daemon; refusing to open a maintenance "
            "epoch while live clients hold hub connections"
        )


def _continue_campaign(
    ctx: click.Context,
    *,
    database_url: str,
    epoch: MaintenanceEpoch,
    batch: DestructiveBatch,
    executor: CampaignExecutor,
) -> DestructiveBatch:
    if batch.status == "aborted":
        raise click.ClickException(f"Destructive batch {batch.id} was aborted and cannot resume")
    if batch.status == "pending" and batch.backup_manifest_sha256 is None:
        evidence = _run_epoch_backup(ctx, epoch.id)
        batch = record_batch_backup(
            database_url,
            epoch.id,
            batch.id,
            manifest_path=str(evidence.manifest_path),
            manifest_sha256=evidence.manifest_sha256,
        )
    if batch.backup_manifest_sha256 is None:
        raise click.ClickException(
            f"Destructive batch {batch.id} has no epoch-bound backup evidence"
        )
    if batch.status == "pending":
        executor.apply(epoch, batch)
        batch = mark_batch_applied(database_url, epoch.id, batch.id)
    if batch.status == "applied":
        executor.verify(epoch, batch)
        batch = mark_batch_verified(database_url, epoch.id, batch.id)
    return batch


def _finish_or_report(
    ctx: click.Context,
    *,
    database_url: str,
    epoch: MaintenanceEpoch,
    batch: DestructiveBatch,
    executor: CampaignExecutor,
    released_by_command: str,
) -> None:
    # The armed fence admits only epoch-bound logins. Exporting the epoch
    # token here covers every in-process libpq connection the campaign makes
    # after arming (admission probes, CliRuntime reconnects) and is inherited
    # by owner-CLI subprocesses.
    os.environ.update(maintenance_child_environment(epoch.id))
    try:
        batch = _continue_campaign(
            ctx,
            database_url=database_url,
            epoch=epoch,
            batch=batch,
            executor=executor,
        )
        if batch.status != "verified":
            raise click.ClickException(
                f"Destructive batch {batch.id} stopped in state {batch.status}"
            )
        release_maintenance_epoch(
            database_url,
            epoch.id,
            owner_command=epoch.opened_by,
            released_by_command=released_by_command,
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(
            f"Maintenance epoch {epoch.id} remains open for resume: {exc}"
        ) from exc
    if epoch.campaign == "account-identity-cutover":
        click.echo("Install the staged gdaemon and gcode binaries, then start the daemon manually.")
        return
    _start_daemon()


def _run_epoch_backup(ctx: click.Context, epoch_id: object) -> BackupEvidence:
    config_file = ctx.find_root().params.get("config")
    argv = [sys.executable, "-m", "gobby.cli"]
    if isinstance(config_file, str):
        argv.extend(["--config", config_file])
    argv.extend(["hub-backup", "--epoch", str(epoch_id), "--json"])
    result = subprocess.run(  # nosec B603 - fixed interpreter/module and arguments
        argv,
        capture_output=True,
        text=True,
        check=False,
        env=maintenance_child_environment(str(epoch_id)),
    )
    if result.returncode != 0:
        detail = _child_cli_failure_detail(result)
        raise click.ClickException(f"Epoch-bound hub backup failed: {detail}")
    try:
        output = json.loads(result.stdout)
        manifest_path = Path(str(output["manifest"]))
        reported_epoch = str(output["epoch_id"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise click.ClickException("Hub backup returned invalid JSON evidence") from exc
    if reported_epoch != str(epoch_id):
        raise click.ClickException(
            f"Hub backup reported epoch {reported_epoch}, expected {epoch_id}"
        )
    if not manifest_path.is_file():
        raise click.ClickException(f"Hub backup manifest is missing: {manifest_path}")
    with manifest_path.open("rb") as manifest_file:
        digest = hashlib.file_digest(manifest_file, "sha256").hexdigest()
    return BackupEvidence(manifest_path=manifest_path, manifest_sha256=digest)


def _child_cli_failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    stderr = result.stderr.strip()
    lines = stderr.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if line.startswith("Error: "):
            return "\n".join([line.removeprefix("Error: "), *lines[index + 1 :]]).strip()
    return stderr or result.stdout.strip() or "unknown error"


def _load_campaign_executor(campaign: Campaign) -> CampaignExecutor:
    executor = _CAMPAIGN_EXECUTORS.get(campaign)
    if executor is None and campaign == ACCOUNT_IDENTITY_CAMPAIGN:
        from gobby.cli.account_identity_cutover import (
            install_account_identity_cutover_executor,
        )

        install_account_identity_cutover_executor()
        executor = _CAMPAIGN_EXECUTORS.get(campaign)
    if executor is None:
        raise click.ClickException(
            f"Maintenance campaign {campaign!r} has no installed implementation"
        )
    return executor


def _resolve_database_url() -> str:
    return _resolve_backup_database_url(get_gobby_home())


def _epoch_payload(epoch: MaintenanceEpoch) -> dict[str, object]:
    return {
        "id": str(epoch.id),
        "campaign": epoch.campaign,
        "opened_at": epoch.opened_at.isoformat(),
        "opened_by": epoch.opened_by,
        "scope_note": epoch.scope_note,
        "released_at": epoch.released_at.isoformat() if epoch.released_at else None,
        "released_by_command": epoch.released_by_command,
    }


def _batch_payload(batch: DestructiveBatch) -> dict[str, object]:
    return {
        "id": str(batch.id),
        "maintenance_epoch_id": str(batch.maintenance_epoch_id),
        "campaign": batch.campaign,
        "status": batch.status,
        "backup_manifest_path": batch.backup_manifest_path,
        "backup_manifest_sha256": batch.backup_manifest_sha256,
        "intent": batch.intent,
        "migration_plan": batch.migration_plan,
        "target_receipts": batch.target_receipts,
        "created_at": batch.created_at.isoformat(),
        "updated_at": batch.updated_at.isoformat(),
        "verified_at": batch.verified_at.isoformat() if batch.verified_at else None,
        "aborted_at": batch.aborted_at.isoformat() if batch.aborted_at else None,
        "abort_disposition": batch.abort_disposition,
    }
