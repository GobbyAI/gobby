"""Fixture-only coverage for restore integrity and cold-archive recovery."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast
from unittest.mock import Mock, call

import click
import jsonschema
import pytest
from click.testing import CliRunner, Result

from gobby.cli._daemon_services import ServiceStartResult
from gobby.cli.hub_backup import cli as hub_cli
from gobby.cli.hub_backup._manifest import (
    HUB_BACKUP_MANIFEST_SCHEMA_V3,
    MANIFEST_NAME,
    STORE_KEYS,
    ArtifactRecord,
    HubBackupManifest,
    SourceIdentity,
    StoreRecord,
    VerificationState,
    write_manifest,
)
from gobby.cli.hub_backup._stores import GLOBALS_DUMP_RELPATH, HUB_VOLUMES, POSTGRES_DUMP_RELPATH
from gobby.cli.hub_backup.files_home import FILES_ARCHIVE_RELPATH
from gobby.cli.hub_backup.rehearsal import RehearsalProfile

pytestmark = pytest.mark.unit

_CONSUMED_PATHS = (FILES_ARCHIVE_RELPATH, GLOBALS_DUMP_RELPATH, POSTGRES_DUMP_RELPATH)
_TEST_DATABASE_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"


@dataclass
class RestoreFixture:
    root: Path
    manifest: HubBackupManifest
    confirm: Mock
    claim: Mock
    files: Mock
    globals: Mock
    postgres: Mock
    reconcile: Mock

    def invoke(self) -> Result:
        return CliRunner().invoke(
            hub_cli.hub_backup,
            ["restore", str(self.root), "--database-url", _TEST_DATABASE_URL],
        )

    def assert_no_mutation(self) -> None:
        for operation in (self.claim, self.files, self.globals, self.postgres, self.reconcile):
            operation.assert_not_called()


@pytest.fixture
def restore_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RestoreFixture:
    root = tmp_path / "backup"
    artifacts: list[ArtifactRecord] = []
    for relative_path in _CONSUMED_PATHS:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = relative_path.encode()
        path.write_bytes(content)
        artifacts.append(
            ArtifactRecord(
                relative_path, relative_path, hashlib.sha256(content).hexdigest(), len(content)
            )
        )
    verified = VerificationState(True, "fixture", "2026-10-08T00:00:00+00:00")
    manifest = HubBackupManifest(
        created_at="2026-10-08T00:00:00+00:00",
        gobby_version="0.5.0",
        source_identity=SourceIdentity("fixture", "gobby_test", 1),
        backup_starting_head=1,
        row_count_probes={},
        artifacts=artifacts,
        stores={key: StoreRecord(verified, verified, {}) for key in STORE_KEYS},
    )
    write_manifest(manifest, root / MANIFEST_NAME)
    fixture = RestoreFixture(
        root=root,
        manifest=manifest,
        confirm=Mock(return_value=True),
        claim=Mock(return_value=nullcontext()),
        files=Mock(),
        globals=Mock(),
        postgres=Mock(return_value={"database_url": _TEST_DATABASE_URL}),
        reconcile=Mock(return_value=0),
    )
    monkeypatch.setattr(hub_cli, "_daemon_is_running", lambda: False)
    monkeypatch.setattr(hub_cli, "get_gobby_home", lambda: tmp_path / "home")
    monkeypatch.setattr(click, "confirm", fixture.confirm)
    monkeypatch.setattr(hub_cli, "maintenance_claim", fixture.claim)
    monkeypatch.setattr(hub_cli, "restore_hub_files", fixture.files)
    monkeypatch.setattr(hub_cli, "restore_postgres_globals", fixture.globals)
    monkeypatch.setattr(hub_cli, "restore_postgres_backup", fixture.postgres)
    monkeypatch.setattr(hub_cli, "reconcile_restored_principals", fixture.reconcile)
    return fixture


@pytest.mark.parametrize("relative_path", _CONSUMED_PATHS)
@pytest.mark.parametrize("defect", ["missing", "duplicate"])
def test_restore_requires_exactly_one_consumed_artifact_before_confirmation(
    restore_fixture: RestoreFixture, relative_path: str, defect: str
) -> None:
    data = restore_fixture.manifest.to_dict()
    artifacts = [asdict(artifact) for artifact in restore_fixture.manifest.artifacts]
    if defect == "missing":
        artifacts = [artifact for artifact in artifacts if artifact["path"] != relative_path]
    else:
        artifacts.extend(
            artifact for artifact in list(artifacts) if artifact["path"] == relative_path
        )
    data["artifacts"] = artifacts
    (restore_fixture.root / MANIFEST_NAME).write_text(json.dumps(data))

    result = restore_fixture.invoke()

    assert result.exit_code != 0, result.output
    assert relative_path in result.output
    restore_fixture.confirm.assert_not_called()
    restore_fixture.assert_no_mutation()


@pytest.mark.parametrize("relative_path", _CONSUMED_PATHS)
def test_restore_rejects_changed_consumed_bytes_before_confirmation(
    restore_fixture: RestoreFixture, relative_path: str
) -> None:
    (restore_fixture.root / relative_path).write_bytes(b"corrupted fixture bytes")

    result = restore_fixture.invoke()

    assert result.exit_code != 0, result.output
    assert "integrity verification failed" in result.output
    assert relative_path in result.output
    restore_fixture.confirm.assert_not_called()
    restore_fixture.assert_no_mutation()


def test_restore_accepts_complete_manifest(restore_fixture: RestoreFixture, tmp_path: Path) -> None:
    result = restore_fixture.invoke()

    assert result.exit_code == 0, result.output
    restore_fixture.confirm.assert_called_once()
    files_artifact = restore_fixture.manifest.artifacts[0]
    restore_fixture.files.assert_called_once_with(
        restore_fixture.root, expected_sha256=files_artifact.sha256
    )
    restore_fixture.globals.assert_called_once_with(
        _TEST_DATABASE_URL, restore_fixture.root / GLOBALS_DUMP_RELPATH
    )
    restore_fixture.postgres.assert_called_once_with(
        restore_fixture.root / Path(POSTGRES_DUMP_RELPATH).parent,
        clean=False,
        allow_unverified=True,
        gobby_home=tmp_path / "home",
        database_url=_TEST_DATABASE_URL,
    )
    restore_fixture.reconcile.assert_called_once_with(_TEST_DATABASE_URL)


@pytest.mark.parametrize("store_key", STORE_KEYS)
@pytest.mark.parametrize("flag", ["archive_verified", "restore_verified"])
def test_schema_rejects_empty_artifacts_when_any_store_claims_verification(
    restore_fixture: RestoreFixture, store_key: str, flag: str
) -> None:
    data = restore_fixture.manifest.to_dict()
    data["artifacts"] = []
    for store in data["stores"].values():
        for verification_flag in ("archive_verified", "restore_verified"):
            store[verification_flag]["verified"] = False
    data["stores"][store_key][flag]["verified"] = True

    with pytest.raises(jsonschema.ValidationError, match="non-empty"):
        jsonschema.validate(data, HUB_BACKUP_MANIFEST_SCHEMA_V3)


def test_schema_accepts_empty_unverified_manifest(restore_fixture: RestoreFixture) -> None:
    data = restore_fixture.manifest.to_dict()
    data["artifacts"] = []
    for store in data["stores"].values():
        for flag in ("archive_verified", "restore_verified"):
            store[flag]["verified"] = False

    errors = list(jsonschema.Draft202012Validator(HUB_BACKUP_MANIFEST_SCHEMA_V3).iter_errors(data))
    assert errors == []


@pytest.mark.parametrize("restart_outcome", ["success", "failed"])
def test_partial_stop_failure_still_restarts_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restart_outcome: Literal["success", "failed"],
) -> None:
    stop = Mock(return_value=False)
    restart = Mock(return_value=ServiceStartResult(restart_outcome, "fixture restart failure"))
    archive = Mock()
    monkeypatch.setattr(hub_cli, "_services_stop", stop)
    monkeypatch.setattr(hub_cli, "_services_start", restart)
    monkeypatch.setattr(hub_cli, "tar_volumes", archive)

    with pytest.raises(
        click.ClickException, match="Could not stop the managed Docker services"
    ) as exc:
        hub_cli._archive_volumes(tmp_path / "home", tmp_path / "backup")

    stop.assert_called_once_with(tmp_path / "home")
    restart.assert_called_once_with(tmp_path / "home")
    archive.assert_not_called()
    if restart_outcome == "failed":
        assert "fixture restart failure" in str(exc.value)


def test_archive_error_and_restart_failure_are_both_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_cli, "_services_stop", Mock(return_value=True))
    monkeypatch.setattr(
        hub_cli, "tar_volumes", Mock(side_effect=click.ClickException("fixture archive failure"))
    )
    restart = Mock(return_value=ServiceStartResult("failed", "fixture restart failure"))
    monkeypatch.setattr(hub_cli, "_services_start", restart)

    with pytest.raises(click.ClickException, match="fixture archive failure") as exc:
        hub_cli._archive_volumes(tmp_path / "home", tmp_path / "backup")

    restart.assert_called_once_with(tmp_path / "home")
    assert "fixture restart failure" in str(exc.value)


def test_raised_stop_error_still_restarts_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OSError("fixture stop failure")
    stop = Mock(side_effect=failure)
    monkeypatch.setattr(hub_cli, "_services_stop", stop)
    restart = Mock(return_value=ServiceStartResult("success", "started"))
    archive = Mock()
    monkeypatch.setattr(hub_cli, "_services_start", restart)
    monkeypatch.setattr(hub_cli, "tar_volumes", archive)

    with pytest.raises(OSError, match="fixture stop failure") as exc:
        hub_cli._archive_volumes(tmp_path / "home", tmp_path / "backup")

    assert exc.value is failure
    stop.assert_called_once_with(tmp_path / "home")
    restart.assert_called_once_with(tmp_path / "home")
    archive.assert_not_called()


def test_stop_failure_preserved_when_restart_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_cli, "_services_stop", Mock(return_value=False))
    restart = Mock(side_effect=OSError("fixture restart exception"))
    monkeypatch.setattr(hub_cli, "_services_start", restart)

    with pytest.raises(
        click.ClickException, match="Could not stop the managed Docker services"
    ) as exc:
        hub_cli._archive_volumes(tmp_path / "home", tmp_path / "backup")

    restart.assert_called_once_with(tmp_path / "home")
    assert "fixture restart exception" in str(exc.value)
    assert isinstance(exc.value.__cause__, click.ClickException)


def test_interrupted_archive_still_restarts_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = Mock(return_value=True)
    monkeypatch.setattr(hub_cli, "_services_stop", stop)
    interruption = KeyboardInterrupt("interrupted")
    archive = Mock(side_effect=interruption)
    monkeypatch.setattr(hub_cli, "tar_volumes", archive)
    restart = Mock(return_value=ServiceStartResult("success", "started"))
    monkeypatch.setattr(hub_cli, "_services_start", restart)

    with pytest.raises(KeyboardInterrupt, match="interrupted") as exc:
        hub_cli._archive_volumes(tmp_path / "home", tmp_path / "backup")

    assert exc.value is interruption
    stop.assert_called_once_with(tmp_path / "home")
    archive.assert_called_once_with(tmp_path / "backup", HUB_VOLUMES)
    restart.assert_called_once_with(tmp_path / "home")


def test_rehearsal_stop_and_restart_errors_are_both_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = cast(RehearsalProfile, Mock(spec=RehearsalProfile, volumes=("fixture-volume",)))
    control = Mock(
        side_effect=[
            click.ClickException("fixture rehearsal stop failure"),
            click.ClickException("fixture rehearsal restart failure"),
        ]
    )
    archive = Mock()
    monkeypatch.setattr(hub_cli, "control_rehearsal_services", control)
    monkeypatch.setattr(hub_cli, "tar_volumes", archive)

    with pytest.raises(click.ClickException, match="fixture rehearsal stop failure") as exc:
        hub_cli._archive_volumes(
            tmp_path / "home", tmp_path / "backup", volumes=profile.volumes, rehearsal=profile
        )

    assert control.call_args_list == [call(profile, "stop"), call(profile, "start")]
    archive.assert_not_called()
    assert "fixture rehearsal restart failure" in str(exc.value)
    assert isinstance(exc.value.__cause__, click.ClickException)
