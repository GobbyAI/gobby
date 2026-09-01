from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

import gobby.cli.project_checkout_cutover as command
from gobby.storage.maintenance_epoch import DestructiveBatch, MaintenanceEpoch

pytestmark = pytest.mark.unit


def _epoch() -> MaintenanceEpoch:
    now = datetime(2026, 8, 31, tzinfo=UTC)
    return MaintenanceEpoch(
        id=uuid.uuid4(),
        campaign="project-checkout-cutover",
        opened_at=now,
        opened_by="test",
        scope_note="test",
        released_at=None,
        released_by_command=None,
    )


def _batch(epoch: MaintenanceEpoch) -> DestructiveBatch:
    now = datetime(2026, 8, 31, tzinfo=UTC)
    return DestructiveBatch(
        id=uuid.uuid4(),
        maintenance_epoch_id=epoch.id,
        campaign="project-checkout-cutover",
        status="applied",
        intent={},
        migration_plan=[],
        target_receipts={},
        backup_manifest_path="/tmp/backup.json",
        backup_manifest_sha256="a" * 64,
        verified_at=None,
        aborted_at=None,
        abort_disposition=None,
        created_at=now,
        updated_at=now,
    )


def _install_verify_fakes(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[str, str]],
    *,
    cutover_error: Exception | None = None,
) -> None:
    def verify_cutover(url: str, *, batch_id: uuid.UUID, target_checksum: str) -> None:
        calls.append(("cutover", url))
        if cutover_error is not None:
            raise cutover_error

    monkeypatch.setattr(
        command, "_bound_database_url", lambda epoch_id: f"postgresql://bound/{epoch_id}"
    )
    monkeypatch.setattr(command, "_target_checksum", lambda: "b" * 64)
    monkeypatch.setattr(command, "verify_project_checkout_cutover", verify_cutover)
    monkeypatch.setattr(command, "apply_schema", lambda url: calls.append(("apply", url)))
    monkeypatch.setattr(command, "verify_schema", lambda url: calls.append(("verify", url)))


def test_verify_stamps_pending_migrations_on_the_bound_hub_before_schema_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The campaign applies the checkout DDL itself, so verify must stamp the migration.

    The hub stays fenced to the epoch until release, so the stamp runs through the
    epoch-bound URL, after the cutover rows are proven and before verify_schema.
    """
    calls: list[tuple[str, str]] = []
    _install_verify_fakes(monkeypatch, calls)
    epoch = _epoch()

    command.ProjectCheckoutCutoverExecutor().verify(epoch, _batch(epoch))

    bound = f"postgresql://bound/{epoch.id}"
    assert calls == [("cutover", bound), ("apply", bound), ("verify", bound)]


def test_verify_does_not_stamp_migrations_when_cutover_rows_fail_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    _install_verify_fakes(monkeypatch, calls, cutover_error=RuntimeError("receipt drift"))
    epoch = _epoch()

    with pytest.raises(RuntimeError, match="receipt drift"):
        command.ProjectCheckoutCutoverExecutor().verify(epoch, _batch(epoch))

    assert calls == [("cutover", f"postgresql://bound/{epoch.id}")]
