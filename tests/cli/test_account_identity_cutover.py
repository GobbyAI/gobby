from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

import gobby.cli.account_identity_cutover as command
from gobby.storage.account_identity_cutover import AccountIdentityCutoverError
from gobby.storage.maintenance_epoch import DestructiveBatch, MaintenanceEpoch


def _epoch() -> MaintenanceEpoch:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    return MaintenanceEpoch(
        id=uuid.uuid4(),
        campaign="account-identity-cutover",
        opened_at=now,
        opened_by="test",
        scope_note="test",
        released_at=None,
        released_by_command=None,
    )


def _batch(epoch: MaintenanceEpoch) -> DestructiveBatch:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    return DestructiveBatch(
        id=uuid.uuid4(),
        maintenance_epoch_id=epoch.id,
        campaign="account-identity-cutover",
        status="pending",
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


def test_collect_identity_normalizes_and_hashes_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = iter(["  Test Operator  ", "  OPERATOR@Example.COM  ", "long-enough-password"])
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: next(prompts))

    identity = command._collect_identity()

    assert identity.name == "Test Operator"
    assert identity.email == "OPERATOR@Example.COM"
    assert identity.password_hash.startswith("$argon2id$")


def test_apply_runs_preflight_before_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    epoch = _epoch()
    monkeypatch.setattr(command, "_bound_database_url", lambda _epoch_id: "postgresql://test")
    monkeypatch.setattr(command, "_target_checksum", lambda: "b" * 64)
    monkeypatch.setattr(command, "account_identity_cutover_already_applied", lambda *a, **k: False)
    monkeypatch.setattr(
        command,
        "preflight_account_identity_cutover",
        lambda _url: (_ for _ in ()).throw(AccountIdentityCutoverError("unsafe predecessor")),
    )
    monkeypatch.setattr(
        command,
        "_collect_identity",
        lambda: (_ for _ in ()).throw(AssertionError("prompt must not run")),
    )

    with pytest.raises(AccountIdentityCutoverError, match="unsafe predecessor"):
        command.AccountIdentityCutoverExecutor().apply(epoch, _batch(epoch))


def test_resume_after_commit_skips_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    epoch = _epoch()
    monkeypatch.setattr(command, "_bound_database_url", lambda _epoch_id: "postgresql://test")
    monkeypatch.setattr(command, "_target_checksum", lambda: "b" * 64)
    already_applied = MagicMock(return_value=True)
    monkeypatch.setattr(command, "account_identity_cutover_already_applied", already_applied)
    monkeypatch.setattr(
        command,
        "_collect_identity",
        lambda: (_ for _ in ()).throw(AssertionError("resume must not prompt")),
    )

    batch = _batch(epoch)
    command.AccountIdentityCutoverExecutor().apply(epoch, batch)

    already_applied.assert_called_once_with(
        "postgresql://test",
        batch_id=batch.id,
        target_checksum="b" * 64,
    )


def test_collect_identity_reprompts_confirmation_through_click() -> None:
    runner = CliRunner()

    @click.command()
    def collect() -> None:
        command._collect_identity()

    result = runner.invoke(
        collect,
        input="Test Operator\noperator@example.com\nlong-enough-password\nmismatch\nlong-enough-password\nlong-enough-password\n",
    )

    assert result.exit_code == 0
    assert "Error: The two entered values do not match." in result.output
