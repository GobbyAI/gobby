from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

import gobby.cli.account_identity_cutover as command
from gobby.storage.account_identity_cutover import AccountIdentityCutoverError
from gobby.storage.maintenance_epoch import DestructiveBatch, MaintenanceEpoch

pytestmark = pytest.mark.unit


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


def _install_executor_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    already_applied: Callable[..., bool],
    preflight: Callable[[str], object] | None = None,
) -> None:
    monkeypatch.setattr(command, "_bound_database_url", lambda _epoch_id: "postgresql://test")
    monkeypatch.setattr(command, "_target_checksum", lambda: "b" * 64)
    monkeypatch.setattr(command, "account_identity_cutover_already_applied", already_applied)
    if preflight is not None:
        monkeypatch.setattr(command, "preflight_account_identity_cutover", preflight)


def test_collect_identity_normalizes_and_hashes_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = iter(["  Test Operator  ", "  OPERATOR@Example.COM  ", "long-enough-password"])
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: next(prompts))

    identity = command._collect_identity()

    assert identity.name == "Test Operator"
    assert identity.email == "OPERATOR@Example.COM"
    assert identity.password_hash.startswith("$argon2id$")


def test_collect_identity_reprompts_for_invalid_email_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hash_password = MagicMock(return_value="$argon2id$valid")
    monkeypatch.setattr(command, "hash_password", hash_password)

    runner = CliRunner()

    @click.command()
    def collect() -> None:
        identity = command._collect_identity()
        click.echo(identity.email)

    result = runner.invoke(
        collect,
        input=(
            "Test Operator\n"
            "missing-domain\n"
            "operator@example.com\n"
            "long-enough-password\n"
            "long-enough-password\n"
        ),
    )

    assert result.exit_code == 0, result.output
    assert "valid email address" in result.output
    assert result.output.rstrip().endswith("operator@example.com")
    hash_password.assert_called_once_with("long-enough-password")


def test_apply_runs_preflight_before_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    epoch = _epoch()

    def refuse_preflight(_database_url: str) -> object:
        raise AccountIdentityCutoverError("unsafe predecessor")

    _install_executor_fakes(
        monkeypatch,
        already_applied=lambda *_args, **_kwargs: False,
        preflight=refuse_preflight,
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
    already_applied_calls: list[tuple[str, dict[str, object]]] = []

    def already_applied(database_url: str, **kwargs: object) -> bool:
        already_applied_calls.append((database_url, kwargs))
        return True

    _install_executor_fakes(monkeypatch, already_applied=already_applied)
    monkeypatch.setattr(
        command,
        "_collect_identity",
        lambda: (_ for _ in ()).throw(AssertionError("resume must not prompt")),
    )

    batch = _batch(epoch)
    command.AccountIdentityCutoverExecutor().apply(epoch, batch)

    assert already_applied_calls == [
        (
            "postgresql://test",
            {"batch_id": batch.id, "target_checksum": "b" * 64},
        )
    ]


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
