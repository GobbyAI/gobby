"""`gobby hub-maintenance` lifecycle orchestration."""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.cli.hub_backup.cli import hub_backup
from gobby.storage.maintenance_epoch import (
    BatchStatus,
    Campaign,
    DestructiveBatch,
    MaintenanceEpoch,
)

command = import_module("gobby.cli.hub_maintenance")


def _record[T](events: list[str], event: str, value: T) -> T:
    events.append(event)
    return value


def _capture_abort(
    calls: list[dict[str, Any]],
    kwargs: dict[str, Any],
    epoch: MaintenanceEpoch,
) -> MaintenanceEpoch:
    calls.append(kwargs)
    return epoch


def _epoch(campaign: Campaign = "purge") -> MaintenanceEpoch:
    return MaintenanceEpoch(
        id=uuid.uuid4(),
        campaign=campaign,
        opened_at=datetime.now(UTC),
        opened_by=f"hub-maintenance:{campaign}",
        scope_note=f"{campaign} campaign",
        released_at=None,
        released_by_command=None,
    )


def _batch(
    epoch: MaintenanceEpoch,
    *,
    status: BatchStatus = "pending",
) -> DestructiveBatch:
    return DestructiveBatch(
        id=uuid.uuid4(),
        maintenance_epoch_id=epoch.id,
        campaign=epoch.campaign,
        status=status,
        backup_manifest_path=None,
        backup_manifest_sha256=None,
        intent={"campaign": epoch.campaign},
        migration_plan=[],
        target_receipts={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        verified_at=None,
        aborted_at=None,
        abort_disposition=None,
    )


def _install_lifecycle_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    epoch: MaintenanceEpoch,
    batch: DestructiveBatch,
    events: list[str],
) -> None:
    monkeypatch.setattr(
        command,
        "_resolve_database_url",
        lambda: "postgresql://example/gobby_test",
    )
    monkeypatch.setattr(
        command,
        "stop_daemon",
        lambda **_kwargs: _record(events, "stop-daemon", True),
    )
    monkeypatch.setattr(
        command,
        "open_maintenance_epoch",
        lambda *_args, **_kwargs: _record(events, "open", epoch),
    )
    monkeypatch.setattr(
        command,
        "create_destructive_batch",
        lambda *_args, **_kwargs: _record(events, "batch", batch),
    )
    monkeypatch.setattr(
        command,
        "_run_epoch_backup",
        lambda *_args, **_kwargs: _record(
            events,
            "backup",
            command.BackupEvidence(Path("/tmp/manifest.json"), "a" * 64),
        ),
    )
    monkeypatch.setattr(
        command,
        "record_batch_backup",
        lambda *_args, **_kwargs: _record(
            events,
            "record-backup",
            replace(
                batch,
                backup_manifest_path="/tmp/manifest.json",
                backup_manifest_sha256="a" * 64,
            ),
        ),
    )

    class FakeExecutor:
        def apply(self, _epoch: MaintenanceEpoch, _batch: DestructiveBatch) -> None:
            events.append("apply")

        def verify(self, _epoch: MaintenanceEpoch, _batch: DestructiveBatch) -> None:
            events.append("verify")

    monkeypatch.setattr(command, "_load_campaign_executor", lambda _campaign: FakeExecutor())
    monkeypatch.setattr(
        command,
        "mark_batch_applied",
        lambda *_args, **_kwargs: _record(
            events,
            "mark-applied",
            replace(batch, status="applied"),
        ),
    )
    monkeypatch.setattr(
        command,
        "mark_batch_verified",
        lambda *_args, **_kwargs: _record(
            events,
            "mark-verified",
            replace(batch, status="verified", verified_at=datetime.now(UTC)),
        ),
    )
    monkeypatch.setattr(
        command,
        "release_maintenance_epoch",
        lambda *_args, **_kwargs: _record(events, "release", epoch),
    )
    monkeypatch.setattr(command, "_start_daemon", lambda: events.append("restart"))


@pytest.mark.parametrize(
    "arguments",
    [
        ["hub-maintenance", "run", "purge"],
        ["hub-maintenance", "resume"],
    ],
    ids=["run", "resume"],
)
def test_protected_maintenance_refuses_non_rehearsal_database_before_state_access(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    events: list[str] = []

    def record_unexpected_call(*_args: object, **_kwargs: object) -> None:
        events.append("unexpected")

    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setattr(
        command,
        "_resolve_database_url",
        lambda: "postgresql://operator:secret@example/gobby",
    )
    monkeypatch.setattr(command, "_load_campaign_executor", lambda _campaign: object())
    monkeypatch.setattr(command, "stop_daemon", record_unexpected_call)
    monkeypatch.setattr(command, "open_maintenance_epoch", record_unexpected_call)
    monkeypatch.setattr(command, "create_destructive_batch", record_unexpected_call)
    monkeypatch.setattr(command, "discover_active_maintenance_epoch", record_unexpected_call)
    monkeypatch.setattr(command, "get_destructive_batch", record_unexpected_call)

    result = CliRunner().invoke(cli, arguments)

    assert result.exit_code != 0
    assert "GOBBY_TEST_PROTECT" in result.output
    assert "gobby_test" in result.output
    assert "secret" not in result.output
    assert events == []


def test_protected_maintenance_allows_exact_rehearsal_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setattr(
        command,
        "stop_daemon",
        lambda **_kwargs: _record(events, "stop-daemon", True),
    )

    command._stop_daemon_before_fence("postgresql://example/gobby_test")

    assert events == ["stop-daemon"]


def test_unprotected_maintenance_allows_non_rehearsal_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)
    monkeypatch.setattr(
        command,
        "stop_daemon",
        lambda **_kwargs: _record(events, "stop-daemon", True),
    )

    command._stop_daemon_before_fence("postgresql://example/gobby")

    assert events == ["stop-daemon"]


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://example",
        "host=example",
        "not a dsn",
        "postgresql://operator:secret@example/%zz",
    ],
    ids=["uri-missing-name", "keyword-missing-name", "invalid-dsn", "malformed-name"],
)
def test_protected_maintenance_fails_closed_for_missing_or_malformed_database_name(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    events: list[str] = []
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setattr(
        command,
        "stop_daemon",
        lambda **_kwargs: _record(events, "stop-daemon", True),
    )

    with pytest.raises(click.ClickException, match="GOBBY_TEST_PROTECT") as exc_info:
        command._stop_daemon_before_fence(database_url)

    assert "gobby_test" in exc_info.value.message
    assert "secret" not in exc_info.value.message
    assert events == []


def test_run_owns_open_backup_apply_verify_release_and_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    epoch = _epoch()
    batch = _batch(epoch)
    _install_lifecycle_fakes(monkeypatch, epoch=epoch, batch=batch, events=events)

    result = CliRunner().invoke(cli, ["hub-maintenance", "run", "purge"])

    assert result.exit_code == 0, result.output
    assert events == [
        "stop-daemon",
        "open",
        "batch",
        "backup",
        "record-backup",
        "apply",
        "mark-applied",
        "verify",
        "mark-verified",
        "release",
        "restart",
    ]
    assert str(epoch.id) in result.output


def test_account_identity_cutover_leaves_daemon_stopped_for_binary_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    epoch = _epoch("account-identity-cutover")
    batch = _batch(epoch)
    _install_lifecycle_fakes(monkeypatch, epoch=epoch, batch=batch, events=events)

    result = CliRunner().invoke(
        cli,
        ["hub-maintenance", "run", "account-identity-cutover"],
    )

    assert result.exit_code == 0, result.output
    assert events[-2:] == ["mark-verified", "release"]
    assert "restart" not in events
    assert "Install the staged gdaemon and gcode binaries" in result.output


def test_resume_uses_only_hub_epoch_and_batch_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    epoch = _epoch("reconcile")
    batch = _batch(epoch)
    _install_lifecycle_fakes(monkeypatch, epoch=epoch, batch=batch, events=events)
    monkeypatch.setattr(
        command,
        "discover_active_maintenance_epoch",
        lambda _dsn: _record(events, "discover", epoch),
    )
    monkeypatch.setattr(
        command,
        "get_destructive_batch",
        lambda *_args, **_kwargs: _record(events, "load-batch", batch),
    )

    result = CliRunner().invoke(cli, ["hub-maintenance", "resume"])

    assert result.exit_code == 0, result.output
    assert events == [
        "stop-daemon",
        "discover",
        "load-batch",
        "backup",
        "record-backup",
        "apply",
        "mark-applied",
        "verify",
        "mark-verified",
        "release",
        "restart",
    ]
    assert "reconcile" in result.output


def test_account_identity_cutover_resume_leaves_daemon_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    epoch = _epoch("account-identity-cutover")
    batch = replace(
        _batch(epoch, status="applied"),
        backup_manifest_path="/tmp/manifest.json",
        backup_manifest_sha256="a" * 64,
    )
    _install_lifecycle_fakes(monkeypatch, epoch=epoch, batch=batch, events=events)
    monkeypatch.setattr(command, "discover_active_maintenance_epoch", lambda _dsn: epoch)
    monkeypatch.setattr(command, "get_destructive_batch", lambda *_args, **_kwargs: batch)

    result = CliRunner().invoke(cli, ["hub-maintenance", "resume"])

    assert result.exit_code == 0, result.output
    assert "apply" not in events
    assert events[-2:] == ["mark-verified", "release"]
    assert "restart" not in events


def test_run_aborts_before_fence_when_daemon_stop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    epoch = _epoch()
    batch = _batch(epoch)
    _install_lifecycle_fakes(monkeypatch, epoch=epoch, batch=batch, events=events)
    monkeypatch.setattr(
        command,
        "stop_daemon",
        lambda **_kwargs: _record(events, "stop-daemon", False),
    )

    result = CliRunner().invoke(cli, ["hub-maintenance", "run", "purge"])

    assert result.exit_code != 0
    assert "refusing to open a maintenance epoch" in result.output
    assert events == ["stop-daemon"]


def test_interrupted_run_keeps_epoch_open_and_daemon_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    epoch = _epoch()
    batch = _batch(epoch)
    _install_lifecycle_fakes(monkeypatch, epoch=epoch, batch=batch, events=events)

    class FailingExecutor:
        def apply(self, _epoch: MaintenanceEpoch, _batch: DestructiveBatch) -> None:
            events.append("apply")
            raise RuntimeError("injected campaign crash")

        def verify(self, _epoch: MaintenanceEpoch, _batch: DestructiveBatch) -> None:
            raise AssertionError("verify must not run")

    monkeypatch.setattr(command, "_load_campaign_executor", lambda _campaign: FailingExecutor())

    result = CliRunner().invoke(cli, ["hub-maintenance", "run", "purge"])

    assert result.exit_code != 0
    assert "injected campaign crash" in result.output
    assert "release" not in events
    assert "restart" not in events


def test_verification_failure_keeps_epoch_open_and_daemon_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    epoch = _epoch()
    batch = _batch(epoch)
    _install_lifecycle_fakes(monkeypatch, epoch=epoch, batch=batch, events=events)

    class FailingVerifier:
        def apply(self, _epoch: MaintenanceEpoch, _batch: DestructiveBatch) -> None:
            events.append("apply")

        def verify(self, _epoch: MaintenanceEpoch, _batch: DestructiveBatch) -> None:
            events.append("verify")
            raise RuntimeError("injected verification failure")

    monkeypatch.setattr(command, "_load_campaign_executor", lambda _campaign: FailingVerifier())

    result = CliRunner().invoke(cli, ["hub-maintenance", "run", "purge"])

    assert result.exit_code != 0
    assert "injected verification failure" in result.output
    assert events[-3:] == ["apply", "mark-applied", "verify"]
    assert "release" not in events
    assert "restart" not in events


def test_status_reports_open_epoch_and_batch_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = _epoch("reconcile")
    batch = _batch(epoch, status="applied")
    monkeypatch.setattr(command, "_resolve_database_url", lambda: "postgresql://example/gobby")
    monkeypatch.setattr(command, "discover_active_maintenance_epoch", lambda _dsn: epoch)
    monkeypatch.setattr(command, "get_destructive_batch", lambda *_args, **_kwargs: batch)

    result = CliRunner().invoke(cli, ["hub-maintenance", "status", "--json"])

    assert result.exit_code == 0, result.output
    assert f'"id": "{epoch.id}"' in result.output
    assert '"campaign": "reconcile"' in result.output
    assert '"status": "applied"' in result.output


def test_abort_requires_confirmation_and_records_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = _epoch("reconcile")
    batch = _batch(epoch, status="applied")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(command, "_resolve_database_url", lambda: "postgresql://example/gobby")
    monkeypatch.setattr(command, "discover_active_maintenance_epoch", lambda _dsn: epoch)
    monkeypatch.setattr(command, "get_destructive_batch", lambda *_args, **_kwargs: batch)
    monkeypatch.setattr(
        command,
        "abort_maintenance_epoch",
        lambda *_args, **kwargs: _capture_abort(calls, kwargs, epoch),
    )
    monkeypatch.setattr(command, "_start_daemon", lambda: calls.append({"restart": True}))

    rejected = CliRunner().invoke(
        cli,
        [
            "hub-maintenance",
            "abort",
            "--disposition",
            "catalog verified at pre-cutover state",
        ],
        input="n\n",
    )
    accepted = CliRunner().invoke(
        cli,
        [
            "hub-maintenance",
            "abort",
            "--disposition",
            "catalog verified at pre-cutover state",
        ],
        input="y\n",
    )

    assert rejected.exit_code != 0
    assert calls == [
        {
            "disposition": "catalog verified at pre-cutover state",
            "confirmed": True,
        },
        {"restart": True},
    ]
    assert accepted.exit_code == 0, accepted.output


def test_hub_backup_epoch_refuses_non_orchestrator_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_MAINTENANCE_EPOCH", raising=False)

    result = CliRunner().invoke(hub_backup, ["--epoch", str(uuid.uuid4())])

    assert result.exit_code != 0
    assert "hub-maintenance" in result.output


def test_epoch_backup_failure_surfaces_child_error_after_config_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = (
        "Ignoring removed config_store keys: code_index.auto_index_on_commit\n"
        "Error: Docker pg_dump failed: pg_dump: FATAL: maintenance epoch is active\n"
    )
    completed = subprocess.CompletedProcess[str](
        args=["python", "-m", "gobby.cli", "hub-backup"],
        returncode=1,
        stdout="",
        stderr=stderr,
    )
    monkeypatch.setattr(command.subprocess, "run", lambda *_args, **_kwargs: completed)
    ctx = click.Context(command.hub_maintenance)

    with pytest.raises(click.ClickException) as excinfo:
        command._run_epoch_backup(ctx, uuid.uuid4())

    assert str(excinfo.value) == (
        "Epoch-bound hub backup failed: "
        "Docker pg_dump failed: pg_dump: FATAL: maintenance epoch is active"
    )


def test_identity_cutover_campaign_is_retired() -> None:
    result = CliRunner().invoke(cli, ["hub-maintenance", "run", "identity-cutover"])

    assert result.exit_code == 2
    assert "'identity-cutover' is not one of" in result.output
    assert "identity-cutover" not in command.CAMPAIGNS
    assert "identity-cutover" not in command._CAMPAIGN_EXECUTORS


def test_account_identity_cutover_executor_is_loaded_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.cli.account_identity_cutover import AccountIdentityCutoverExecutor

    monkeypatch.delitem(
        command._CAMPAIGN_EXECUTORS,
        "account-identity-cutover",
        raising=False,
    )

    executor = command._load_campaign_executor("account-identity-cutover")

    assert isinstance(executor, AccountIdentityCutoverExecutor)
