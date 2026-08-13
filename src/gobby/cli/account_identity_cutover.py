"""CLI adapter for the account identity maintenance campaign."""

from __future__ import annotations

import uuid

import click

from gobby.cli.hub_maintenance import CampaignExecutor, register_campaign_executor
from gobby.config.bootstrap import load_bootstrap
from gobby.identity import (
    hash_password,
    normalize_user_email,
    normalize_user_name,
    validate_password,
)
from gobby.storage.account_identity_cutover import (
    ACCOUNT_IDENTITY_CAMPAIGN,
    AccountIdentity,
    account_identity_cutover_already_applied,
    apply_account_identity_cutover,
    preflight_account_identity_cutover,
    verify_account_identity_cutover,
)
from gobby.storage.maintenance_epoch import (
    DestructiveBatch,
    MaintenanceEpoch,
    bind_maintenance_epoch,
)
from gobby.storage.schema_contract import expected_schema_identity, verify_schema


class AccountIdentityCutoverExecutor(CampaignExecutor):
    """Collect credentials, apply the transition, and verify release identity."""

    def apply(self, epoch: MaintenanceEpoch, batch: DestructiveBatch) -> None:
        database_url = _bound_database_url(epoch.id)
        target_checksum = _target_checksum()
        if account_identity_cutover_already_applied(
            database_url,
            batch_id=batch.id,
            target_checksum=target_checksum,
        ):
            return
        preflight = preflight_account_identity_cutover(database_url)
        identity = _collect_identity()
        apply_account_identity_cutover(
            database_url,
            epoch_id=epoch.id,
            batch_id=batch.id,
            identity=identity,
            preflight=preflight,
            target_checksum=target_checksum,
        )

    def verify(self, epoch: MaintenanceEpoch, batch: DestructiveBatch) -> None:
        database_url = _bound_database_url(epoch.id)
        verify_account_identity_cutover(
            database_url,
            batch_id=batch.id,
            target_checksum=_target_checksum(),
        )
        verify_schema(database_url)


def install_account_identity_cutover_executor() -> None:
    """Install the campaign executor into the maintenance registry."""
    register_campaign_executor(ACCOUNT_IDENTITY_CAMPAIGN, AccountIdentityCutoverExecutor())


def _collect_identity() -> AccountIdentity:
    name = normalize_user_name(str(click.prompt("Name")))
    email = normalize_user_email(str(click.prompt("Email")))
    password = validate_password(
        str(click.prompt("Password", hide_input=True, confirmation_prompt=True))
    )
    return AccountIdentity(
        id=uuid.uuid4(),
        name=name,
        email=email,
        password_hash=hash_password(password),
    )


def _bound_database_url(epoch_id: uuid.UUID) -> str:
    bootstrap = load_bootstrap(resolve_database_url=True)
    if not bootstrap.database_url:
        raise click.ClickException("bootstrap.yaml must define database_url")
    return bind_maintenance_epoch(bootstrap.database_url, epoch_id)


def _target_checksum() -> str:
    checksum = expected_schema_identity()["baseline_checksum"]
    if not isinstance(checksum, str):
        raise click.ClickException("Packaged baseline checksum must be a string")
    return checksum
