"""Project-checkout cutover campaign adapter."""

from __future__ import annotations

import uuid

import click

from gobby.cli.hub_maintenance import CampaignExecutor, register_campaign_executor
from gobby.config.bootstrap import load_bootstrap
from gobby.storage.maintenance_epoch import (
    DestructiveBatch,
    MaintenanceEpoch,
    bind_maintenance_epoch,
)
from gobby.storage.project_checkout_cutover import (
    PROJECT_CHECKOUT_CUTOVER_CAMPAIGN,
    apply_project_checkout_cutover,
    preflight_project_checkout_cutover,
    project_checkout_cutover_already_applied,
    record_project_checkout_preflight,
    verify_project_checkout_cutover,
)
from gobby.storage.schema_contract import expected_schema_identity, verify_schema


class ProjectCheckoutCutoverExecutor(CampaignExecutor):
    """Run and verify the prompt-free project-checkout transition."""

    def apply(self, epoch: MaintenanceEpoch, batch: DestructiveBatch) -> None:
        database_url = _bound_database_url(epoch.id)
        target_checksum = _target_checksum()
        if project_checkout_cutover_already_applied(
            database_url,
            batch_id=batch.id,
            target_checksum=target_checksum,
        ):
            return
        preflight = preflight_project_checkout_cutover(database_url)
        record_project_checkout_preflight(
            database_url,
            epoch_id=epoch.id,
            batch_id=batch.id,
            preflight=preflight,
        )
        apply_project_checkout_cutover(
            database_url,
            epoch_id=epoch.id,
            batch_id=batch.id,
            preflight=preflight,
            target_checksum=target_checksum,
        )

    def verify(self, epoch: MaintenanceEpoch, batch: DestructiveBatch) -> None:
        database_url = _bound_database_url(epoch.id)
        verify_project_checkout_cutover(
            database_url,
            batch_id=batch.id,
            target_checksum=_target_checksum(),
        )
        verify_schema(database_url)


def install_project_checkout_cutover_executor() -> None:
    register_campaign_executor(
        PROJECT_CHECKOUT_CUTOVER_CAMPAIGN,
        ProjectCheckoutCutoverExecutor(),
    )


def _bound_database_url(epoch_id: uuid.UUID) -> str:
    bootstrap = load_bootstrap(resolve_database_url=True)
    if not bootstrap.database_url:
        raise click.ClickException("bootstrap.yaml must define database_url")
    return bind_maintenance_epoch(bootstrap.database_url, epoch_id)


def _target_checksum() -> str:
    checksum = expected_schema_identity()["baseline_checksum"]
    if not isinstance(checksum, str):
        raise click.ClickException("Expected schema baseline checksum is invalid")
    return checksum
