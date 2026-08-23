"""Tests for the `gobby hub-backup` orchestration command.

No real Docker, daemon, or datastore is touched. Every collaborator the command
orchestrates is replaced by a recording fake, so these tests pin the
orchestration contract — step order, cleanup guarantees, and manifest
contents — rather than any store-driver or verifier behaviour.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, cast
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner, Result

from gobby.cli import cli as root_cli
from gobby.cli._daemon_services import ServiceStartResult
from gobby.cli.hub_backup import cli as hub_cli
from gobby.cli.hub_backup._manifest import (
    MANIFEST_FORMAT,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    ArtifactRecord,
    SourceIdentity,
    VerificationState,
    load_manifest,
)
from gobby.cli.hub_backup._stores import (
    FALKORDB_DUMP_RELPATH,
    GLOBALS_DUMP_RELPATH,
    HUB_VOLUMES,
    POSTGRES_DUMP_RELPATH,
    VOLUME_ARCHIVE_DIR,
)
from gobby.cli.hub_backup._verify import RoleExpectation
from gobby.cli.hub_backup.files_home import FILES_ARCHIVE_RELPATH
from gobby.cli.installers.compose_env import ComposeRuntime
from gobby.cli.installers.container_restart import FALKORDB_CONTAINER
from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig
from gobby.config.logging import RULE_ALLOW_AUDIT_LOG_FILENAME
from gobby.storage.config_repository import UnknownStoredConfigKeyError
from gobby.storage.maintenance_epoch import MAINTENANCE_EPOCH_ENV

pytestmark = pytest.mark.unit

DATABASE_PASSWORD = "n0t-a-real-password"
DATABASE_URL = f"postgresql://gobby:{DATABASE_PASSWORD}@localhost:60891/gobby"
QDRANT_URL = "http://localhost:6333"
QDRANT_API_KEY = "not-a-real-qdrant-api-key"
SYSTEM_IDENTIFIER = "7412345678901234567"
DATABASE_OID = 16401
STARTING_HEAD = 187

ROW_PROBES = {"tasks": 1204, "sessions": 88}
SCHEMA_OBJECTS = {"table": 22, "index": 37}
# postgres-dump, postgres-globals, one qdrant snapshot, falkordb-rdb, and files.
# The harness home has no machine identity and the runtime fixture points the
# logging dir at an empty tmp path, so identity and rule_allow_audit archives
# (d41adc20c, #19418) contribute nothing here.
NON_VOLUME_ARTIFACTS = 5
QDRANT_COLLECTION = "gobby_memories"
QDRANT_SNAPSHOT_RELPATH = f"qdrant/{QDRANT_COLLECTION}.snapshot"
QDRANT_POINTS = 4211
QDRANT_DIGEST = "c" * 64
FALKORDB_GRAPHS = ["gobby_kg"]
FALKORDB_INVENTORY = {"gobby_kg": {"nodes": 44, "edges": 31}}
VOLUME_INVENTORY = {"members": 9, "sha256": "d" * 64}

SOURCE_ROLES: list[dict[str, object]] = [
    {"rolname": "gobby", "rolsuper": False, "rolcanlogin": True},
    {"rolname": "gobby_ro", "rolsuper": False, "rolcanlogin": False},
]

CONTRACT_ORDER = [
    "resolve_database_url",
    "which:docker",
    "inspect:gobby-postgres",
    "inspect:services-qdrant-1",
    "inspect:services-falkordb-1",
    "disk_usage",
    "require_managed_docker_postgres",
    "daemon_is_running",
    "stop_daemon",
    "collect_postgres_identity",
    "collect_row_count_probes",
    "collect_schema_object_counts",
    "collect_source_roles",
    "dump_postgres",
    "snapshot_qdrant",
    "dump_falkordb",
    "services_stop",
    "tar_volumes",
    "services_start",
    "archive_files_home",
    "verify_postgres_restore",
    "verify_qdrant_restore",
    "verify_falkordb_restore",
    "verify_volume_archives",
    "verify_files_home_archive",
    "start_daemon",
]


class _DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


class _StepFailure(click.ClickException):
    """Injected failure proving the command's cleanup paths still run."""


def _artifact(name: str, relpath: str, backup_root: Path) -> ArtifactRecord:
    content = f"artifact:{name}\n".encode()
    path = backup_root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return ArtifactRecord(
        name=name,
        path=relpath,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def _verified(method: str) -> VerificationState:
    return VerificationState(
        verified=True,
        method=method,
        timestamp="2026-07-31T12:00:00+00:00",
    )


class _Harness:
    """Recording stand-in for every collaborator `hub-backup` orchestrates."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.gobby_home = tmp_path / "gobby-home"
        self.database_url = DATABASE_URL
        self.calls: list[str] = []
        self.running: list[str] = list(hub_cli.REQUIRED_CONTAINERS)
        self.docker_path: str | None = "/usr/local/bin/docker"
        self.free_bytes: int = 500 * 1024**3
        self.daemon_running: bool = True
        self.fail_at: str | None = None
        self.services_stop_result: bool = True
        self.services_start_outcome: str = "success"

        self.gobby_home_seen: Path | None = None
        self.managed_url_seen: str | None = None
        self.dump_url_seen: str | None = None
        self.backup_root_seen: Path | None = None
        self.qdrant_snapshot_settings: tuple[str | None, str | None] | None = None
        self.qdrant_verify_settings: tuple[str | None, str | None] | None = None
        self.shutdown_source_seen: str | None = None
        self.stop_quiet_seen: bool | None = None
        self.postgres_paths_seen: tuple[Path, Path] | None = None
        self.probes_seen: dict[str, int] | None = None
        self.schema_objects_seen: dict[str, int] | None = None
        self.roles_seen: list[RoleExpectation] | None = None
        self.snapshots_seen: dict[str, Path] | None = None
        self.point_counts_seen: dict[str, int] | None = None
        self.point_digests_seen: dict[str, str] | None = None
        self.rdb_path_seen: Path | None = None
        self.falkordb_container_seen: str | None = None
        self.graph_inventory_seen: dict[str, dict[str, int]] | None = None
        self.archives_seen: dict[str, Path] | None = None
        self.volume_inventories_seen: dict[str, dict[str, object]] | None = None
        self._real_subprocess_run = subprocess.run

    # -- recording --------------------------------------------------------

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_at == name:
            raise _StepFailure(f"injected failure at {name}")

    # -- preflight --------------------------------------------------------

    def which(self, name: str) -> str | None:
        self._step(f"which:{name}")
        return self.docker_path

    def disk_usage(self, path: Any) -> _DiskUsage:
        self._step("disk_usage")
        return _DiskUsage(total=self.free_bytes * 2, used=self.free_bytes, free=self.free_bytes)

    def subprocess_run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if args[:2] != ["docker", "inspect"]:
            return self._real_subprocess_run(args, **kwargs)
        assert kwargs.get("timeout"), f"missing subprocess timeout: {args}"
        container = args[-1]
        self._step(f"inspect:{container}")
        running = container in self.running
        return subprocess.CompletedProcess(
            args,
            0 if running else 1,
            b"true\n" if running else b"",
            b"" if running else b"No such object",
        )

    # -- database resolution ----------------------------------------------

    def resolve_database_url(self, gobby_home: Path) -> str:
        self._step("resolve_database_url")
        self.gobby_home_seen = gobby_home
        return self.database_url

    def require_managed_docker_postgres(self, *, database_url: str) -> None:
        self._step("require_managed_docker_postgres")
        self.managed_url_seen = database_url

    # -- daemon lifecycle --------------------------------------------------

    def daemon_is_running(self) -> bool:
        self._step("daemon_is_running")
        return self.daemon_running

    def stop_daemon(
        self,
        quiet: bool = False,
        *,
        shutdown_intent: str = "stop",
        shutdown_source: str = "cli_stop",
    ) -> bool:
        self._step("stop_daemon")
        self.shutdown_source_seen = shutdown_source
        self.stop_quiet_seen = quiet
        return True

    def start_daemon(self) -> None:
        self._step("start_daemon")

    # -- source facts ------------------------------------------------------

    def collect_postgres_identity(self, database_url: str) -> tuple[SourceIdentity, int]:
        self._step("collect_postgres_identity")
        identity = SourceIdentity(
            pg_system_identifier=SYSTEM_IDENTIFIER,
            database_name="gobby",
            database_oid=DATABASE_OID,
        )
        return identity, STARTING_HEAD

    def collect_row_count_probes(self, database_url: str) -> dict[str, int]:
        self._step("collect_row_count_probes")
        return dict(ROW_PROBES)

    def collect_schema_object_counts(self, database_url: str) -> dict[str, int]:
        self._step("collect_schema_object_counts")
        return dict(SCHEMA_OBJECTS)

    def collect_source_roles(self, database_url: str) -> list[dict[str, object]]:
        self._step("collect_source_roles")
        return [dict(role) for role in SOURCE_ROLES]

    # -- store drivers -----------------------------------------------------

    def dump_postgres(
        self, database_url: str, backup_root: Path
    ) -> tuple[list[ArtifactRecord], dict[str, object]]:
        self._step("dump_postgres")
        self.dump_url_seen = database_url
        self.backup_root_seen = backup_root
        artifacts = [
            _artifact("postgres-dump", POSTGRES_DUMP_RELPATH, backup_root),
            _artifact("postgres-globals", GLOBALS_DUMP_RELPATH, backup_root),
        ]
        return artifacts, {"postgres_version": "16.4", "archive_list_checked": True}

    def snapshot_qdrant(
        self, url: str | None, api_key: str | None, backup_root: Path
    ) -> tuple[list[ArtifactRecord], dict[str, object]]:
        self._step("snapshot_qdrant")
        self.qdrant_snapshot_settings = (url, api_key)
        artifacts = [_artifact(f"qdrant-{QDRANT_COLLECTION}", QDRANT_SNAPSHOT_RELPATH, backup_root)]
        details: dict[str, object] = {
            "collections": {
                QDRANT_COLLECTION: {
                    "points": QDRANT_POINTS,
                    "snapshot": QDRANT_SNAPSHOT_RELPATH,
                    "content_sha256": QDRANT_DIGEST,
                }
            }
        }
        return artifacts, details

    def dump_falkordb(
        self,
        backup_root: Path,
        *,
        container: str = FALKORDB_CONTAINER,
    ) -> tuple[list[ArtifactRecord], dict[str, object]]:
        self._step("dump_falkordb")
        self.falkordb_container_seen = container
        artifacts = [_artifact("falkordb-rdb", FALKORDB_DUMP_RELPATH, backup_root)]
        return artifacts, {
            "graphs": list(FALKORDB_GRAPHS),
            "graph_inventory": FALKORDB_INVENTORY,
            "dbsize": 12,
        }

    def services_stop(self, gobby_home: Path) -> bool:
        self._step("services_stop")
        return self.services_stop_result

    def services_start(self, gobby_home: Path) -> ServiceStartResult:
        self._step("services_start")
        detail = "" if self.services_start_outcome == "success" else "compose up failed"
        if self.services_start_outcome == "success":
            return ServiceStartResult("success", detail)
        if self.services_start_outcome == "skipped":
            return ServiceStartResult("skipped", detail)
        return ServiceStartResult("failed", detail)

    def tar_volumes(
        self, backup_root: Path, volumes: Any = HUB_VOLUMES
    ) -> tuple[list[ArtifactRecord], dict[str, object]]:
        self._step("tar_volumes")
        names = list(volumes)
        artifacts = [
            _artifact(
                f"volume-{name}",
                f"{VOLUME_ARCHIVE_DIR}/{name}.tar.gz",
                backup_root,
            )
            for name in names
        ]
        return artifacts, {
            "volumes": names,
            "source_inventories": {name: dict(VOLUME_INVENTORY) for name in names},
        }

    def archive_files_home_store(
        self, backup_root: Path, files_home: Path | None = None
    ) -> tuple[list[ArtifactRecord], dict[str, object]]:
        del files_home
        self._step("archive_files_home")
        return [_artifact("files-home", FILES_ARCHIVE_RELPATH, backup_root)], {"members": 3}

    # -- verifiers ---------------------------------------------------------

    def verify_postgres_restore(
        self,
        dump_path: Path,
        globals_path: Path,
        *,
        expected_probes: dict[str, int],
        expected_roles: list[RoleExpectation],
        expected_schema_objects: dict[str, int],
    ) -> tuple[VerificationState, dict[str, object]]:
        self._step("verify_postgres_restore")
        self.postgres_paths_seen = (dump_path, globals_path)
        self.probes_seen = dict(expected_probes)
        self.roles_seen = list(expected_roles)
        self.schema_objects_seen = dict(expected_schema_objects)
        return _verified("pg-scratch-restore"), {"tables_checked": len(expected_probes)}

    def verify_qdrant_restore(
        self,
        url: str | None,
        api_key: str | None,
        snapshots: dict[str, Path],
        expected_counts: dict[str, int],
        expected_digests: dict[str, str],
    ) -> tuple[VerificationState, dict[str, object]]:
        self._step("verify_qdrant_restore")
        self.qdrant_verify_settings = (url, api_key)
        self.snapshots_seen = dict(snapshots)
        self.point_counts_seen = dict(expected_counts)
        self.point_digests_seen = dict(expected_digests)
        return _verified("qdrant-snapshot-recover"), {"collections_checked": len(snapshots)}

    def verify_falkordb_restore(
        self, rdb_path: Path, expected_inventory: dict[str, dict[str, int]]
    ) -> tuple[VerificationState, dict[str, object]]:
        self._step("verify_falkordb_restore")
        self.rdb_path_seen = rdb_path
        self.graph_inventory_seen = dict(expected_inventory)
        return _verified("falkordb-scratch-load"), {"graphs_verified": len(expected_inventory)}

    def verify_volume_archives(
        self,
        archives: dict[str, Path],
        expected_inventories: dict[str, dict[str, object]],
    ) -> tuple[VerificationState, dict[str, object]]:
        self._step("verify_volume_archives")
        self.archives_seen = dict(archives)
        self.volume_inventories_seen = dict(expected_inventories)
        counts = dict.fromkeys(archives, 9)
        return _verified("tar-extract"), {"archives": counts}

    def verify_files_home_archive(
        self, archive: Path
    ) -> tuple[VerificationState, dict[str, object]]:
        self._step("verify_files_home_archive")
        del archive
        return _verified("files-home-scratch"), {"members": 3}

    # -- wiring ------------------------------------------------------------

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._real_subprocess_run = subprocess.run
        monkeypatch.setattr(shutil, "which", self.which)
        monkeypatch.setattr(shutil, "disk_usage", self.disk_usage)
        monkeypatch.setattr(subprocess, "run", self.subprocess_run)
        monkeypatch.setattr(hub_cli, "get_gobby_home", lambda: self.gobby_home)
        replacements: dict[str, object] = {
            "_resolve_database_url": self.resolve_database_url,
            "require_orchestrator_epoch": lambda _database_url, _epoch: None,
            "_require_managed_docker_postgres": self.require_managed_docker_postgres,
            "_daemon_is_running": self.daemon_is_running,
            "stop_daemon": self.stop_daemon,
            "_start_daemon": self.start_daemon,
            "collect_postgres_identity": self.collect_postgres_identity,
            "collect_row_count_probes": self.collect_row_count_probes,
            "collect_schema_object_counts": self.collect_schema_object_counts,
            "collect_source_roles": self.collect_source_roles,
            "dump_postgres": self.dump_postgres,
            "snapshot_qdrant": self.snapshot_qdrant,
            "dump_falkordb": self.dump_falkordb,
            "_services_stop": self.services_stop,
            "_services_start": self.services_start,
            "_start_epoch_services": self.services_start,
            "tar_volumes": self.tar_volumes,
            "archive_files_home_store": self.archive_files_home_store,
            "verify_postgres_restore": self.verify_postgres_restore,
            "verify_qdrant_restore": self.verify_qdrant_restore,
            "verify_falkordb_restore": self.verify_falkordb_restore,
            "verify_volume_archives": self.verify_volume_archives,
            "verify_files_home_archive": self.verify_files_home_archive,
        }
        for name, replacement in replacements.items():
            monkeypatch.setattr(hub_cli, name, replacement)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Harness:
    instance = _Harness(tmp_path)
    instance.gobby_home.mkdir(parents=True)
    monkeypatch.setenv("GOBBY_HOME", str(instance.gobby_home))
    instance.install(monkeypatch)
    return instance


@pytest.fixture
def runtime(tmp_path: Path) -> CliRuntime:
    config = DaemonConfig()
    config.databases.qdrant.url = QDRANT_URL
    config.databases.qdrant.api_key = QDRANT_API_KEY
    # Isolate from the real ~/.gobby/logs so machine-local rule_allow_audit
    # rotations cannot leak artifacts into backup manifests.
    config.logging.dir = str(tmp_path / "isolated-logs")
    return CliRuntime(config_file=None, config=config)


def _invoke(runtime: CliRuntime, *args: str) -> Result:
    return CliRunner().invoke(hub_cli.hub_backup, list(args), obj=runtime)


def _run_ok(runtime: CliRuntime, backup_root: Path, *extra: str) -> Result:
    result = _invoke(runtime, "--output", str(backup_root), *extra)
    assert result.exit_code == 0, result.output
    return result


class TestRegistration:
    def test_hub_backup_is_registered_on_the_root_cli(self) -> None:
        assert "hub-backup" in root_cli.commands
        assert root_cli.commands["hub-backup"] is hub_cli.hub_backup


class TestRestore:
    def test_restore_uses_explicit_target_and_verified_hub_artifact(
        self,
        harness: _Harness,
        runtime: CliRuntime,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        backup_root = tmp_path / "backup"
        _run_ok(runtime, backup_root)
        calls: list[tuple[Path, dict[str, object]]] = []
        restore_steps: list[str] = []

        def restore_globals(_database_url: str, _globals_path: Path) -> None:
            restore_steps.append("globals")

        def reconcile_principals(_database_url: str) -> int:
            restore_steps.append("reconcile")
            return 1

        def restore_postgres(source: Path, **kwargs: object) -> dict[str, object]:
            restore_steps.append("data")
            calls.append((source, kwargs))
            return {
                "database_url": "postgresql://gobby:****@target:5432/gobby",
                "released_epoch_id": "11111111-1111-1111-1111-111111111111",
            }

        monkeypatch.setattr(hub_cli, "restore_postgres_globals", restore_globals, raising=False)
        monkeypatch.setattr(
            hub_cli,
            "reconcile_restored_principals",
            reconcile_principals,
            raising=False,
        )
        monkeypatch.setattr(hub_cli, "restore_postgres_backup", restore_postgres)
        monkeypatch.setattr(hub_cli, "restore_hub_files", lambda *_a, **_k: None)
        monkeypatch.setattr(hub_cli, "_daemon_is_running", lambda: False)
        monkeypatch.setattr(
            hub_cli,
            "_resolve_database_url",
            lambda _home: pytest.fail("restore must not resolve the origin database"),
        )

        result = _invoke(
            runtime,
            "restore",
            str(backup_root),
            "--database-url",
            "postgresql://gobby:secret@target:5432/gobby",
            "--yes",
        )

        assert result.exit_code == 0, result.output
        assert restore_steps == ["globals", "data", "reconcile"]
        assert calls == [
            (
                backup_root / "postgres",
                {
                    "clean": False,
                    "allow_unverified": True,
                    "gobby_home": harness.gobby_home,
                    "database_url": "postgresql://gobby:secret@target:5432/gobby",
                },
            )
        ]
        assert "Maintenance epoch released by restore" in result.output

    def test_6_2_17_snapshot_holds_maintenance_claim(
        self,
        harness: _Harness,
        runtime: CliRuntime,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.cli.hub_backup import files_home as files_home_mod
        from gobby.cli.hub_backup.files_home import FilesHomeArchiveHooks
        from gobby.runner_pid_file import claim_pid_file

        seen: list[bool] = []

        def _on_claimed(claim: object) -> None:
            del claim
            blocked = claim_pid_file(harness.gobby_home / "gobby.pid", role="daemon")
            seen.append(blocked is None)
            if blocked is not None:
                blocked.release()

        monkeypatch.setattr(
            files_home_mod,
            "_active_hooks",
            FilesHomeArchiveHooks(on_claimed=_on_claimed),
        )
        _run_ok(runtime, tmp_path / "backup")
        assert seen == [True]

    def test_6_2_28_injected_files_store_failure_preserves_dest(
        self,
        harness: _Harness,
        runtime: CliRuntime,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.cli.hub_backup.files_home import FilesHomeArchiveError

        dest = tmp_path / "backup"
        dest.mkdir()
        (dest / "prior.txt").write_text("keep", encoding="utf-8")

        def _fail(
            backup_root: Path, files_home: Path | None = None
        ) -> tuple[list[ArtifactRecord], dict[str, object]]:
            del backup_root, files_home
            harness.calls.append("archive_files_home")
            raise FilesHomeArchiveError("temp_write", "injected temp write failure")

        monkeypatch.setattr(hub_cli, "archive_files_home_store", _fail)
        result = _invoke(runtime, "--output", str(dest))
        assert result.exit_code != 0
        assert (dest / "prior.txt").read_text(encoding="utf-8") == "keep"


class TestOrchestration:
    def test_full_success_follows_the_contract_order(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        _run_ok(runtime, tmp_path / "backup")

        assert harness.calls == CONTRACT_ORDER

    def test_resolved_dsn_flows_into_the_managed_check_and_the_dump(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        _run_ok(runtime, tmp_path / "backup")

        assert harness.gobby_home_seen == harness.gobby_home
        assert harness.managed_url_seen == DATABASE_URL
        assert harness.dump_url_seen == DATABASE_URL
        assert harness.shutdown_source_seen == "cli_hub_backup"

    def test_qdrant_settings_come_from_the_cli_runtime_config(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        _run_ok(runtime, tmp_path / "backup")

        assert harness.qdrant_snapshot_settings == (QDRANT_URL, QDRANT_API_KEY)
        assert harness.qdrant_verify_settings == (QDRANT_URL, QDRANT_API_KEY)

    def test_epoch_qdrant_settings_read_only_required_predecessor_rows(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queries: list[str] = []

        class _Database:
            def fetchall(self, query: str) -> list[dict[str, str]]:
                queries.append(query)
                if query.strip() == "SELECT key FROM config_store ORDER BY key":
                    return [
                        {"key": "auth.password_hash"},
                        {"key": "auth.username"},
                        {"key": "databases.qdrant.url"},
                    ]
                return [
                    {
                        "key": "databases.qdrant.url",
                        "value": '"http://127.0.0.1:60990"',
                    }
                ]

        class _Runtime:
            def __init__(self) -> None:
                self.database_calls: list[bool] = []
                self.config_calls = 0

            def require_database(self, *, apply_migrations: bool = True) -> object:
                self.database_calls.append(apply_migrations)
                return _Database()

            def require_config(self, *, apply_migrations: bool = True) -> object:
                self.config_calls += 1
                raise AssertionError("predecessor path must not load full config")

        runtime = _Runtime()
        monkeypatch.setattr(hub_cli, "get_cli_runtime", lambda _ctx: cast(Any, runtime))

        settings = hub_cli._qdrant_settings(cast(Any, object()), apply_migrations=False)

        assert settings == ("http://127.0.0.1:60990", None)
        assert runtime.database_calls == [False]
        assert runtime.config_calls == 0
        assert len(queries) == 2
        assert "databases.qdrant.api_key" in queries[1]

    def test_epoch_qdrant_settings_reject_other_unknown_config_keys(self) -> None:
        database = MagicMock()
        database.fetchall.return_value = [{"key": "removed.setting"}]

        with pytest.raises(UnknownStoredConfigKeyError, match="removed.setting"):
            hub_cli._predecessor_qdrant_settings(database)

    def test_protected_scratch_backup_routes_all_docker_state_to_scratch(
        self,
        harness: _Harness,
        runtime: CliRuntime,
        tmp_path: Path,
    ) -> None:
        harness.database_url = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
        harness.running = [
            "gobby-postgres-test-1",
            "gobby-qdrant-test-1",
            "gobby-falkordb-test-1",
        ]
        runtime.config.databases.qdrant.url = "http://127.0.0.1:60990"

        _run_ok(runtime, tmp_path / "scratch-backup")

        assert harness.falkordb_container_seen == "gobby-falkordb-test-1"
        assert harness.volume_inventories_seen == dict.fromkeys(
            hub_cli.SCRATCH_HUB_VOLUMES,
            VOLUME_INVENTORY,
        )
        assert not set(harness.volume_inventories_seen) & set(HUB_VOLUMES)

    def test_protected_scratch_backup_refuses_non_scratch_qdrant(
        self,
        harness: _Harness,
        runtime: CliRuntime,
        tmp_path: Path,
    ) -> None:
        harness.database_url = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
        harness.running = [
            "gobby-postgres-test-1",
            "gobby-qdrant-test-1",
            "gobby-falkordb-test-1",
        ]

        result = _invoke(runtime, "--output", str(tmp_path / "scratch-backup"))

        assert result.exit_code != 0
        assert "loopback port 60990" in result.output
        assert "stop_daemon" not in harness.calls

    def test_verifiers_receive_driver_derived_expectations(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        backup_root = tmp_path / "backup"
        _run_ok(runtime, backup_root)

        assert harness.backup_root_seen is not None
        assert harness.postgres_paths_seen is not None
        assert tuple(
            path.relative_to(harness.backup_root_seen) for path in harness.postgres_paths_seen
        ) == (
            Path(POSTGRES_DUMP_RELPATH),
            Path(GLOBALS_DUMP_RELPATH),
        )
        assert harness.probes_seen == ROW_PROBES
        assert harness.schema_objects_seen == SCHEMA_OBJECTS
        assert harness.roles_seen == [
            RoleExpectation(rolname="gobby", rolsuper=False, rolcanlogin=True),
            RoleExpectation(rolname="gobby_ro", rolsuper=False, rolcanlogin=False),
        ]
        assert harness.snapshots_seen is not None
        assert {
            name: path.relative_to(harness.backup_root_seen)
            for name, path in harness.snapshots_seen.items()
        } == {QDRANT_COLLECTION: Path(QDRANT_SNAPSHOT_RELPATH)}
        assert harness.point_counts_seen == {QDRANT_COLLECTION: QDRANT_POINTS}
        assert harness.point_digests_seen == {QDRANT_COLLECTION: QDRANT_DIGEST}
        assert harness.rdb_path_seen is not None
        assert harness.rdb_path_seen.relative_to(harness.backup_root_seen) == Path(
            FALKORDB_DUMP_RELPATH
        )
        assert harness.graph_inventory_seen == FALKORDB_INVENTORY
        assert harness.archives_seen is not None
        assert {
            volume: path.relative_to(harness.backup_root_seen)
            for volume, path in harness.archives_seen.items()
        } == {volume: Path(VOLUME_ARCHIVE_DIR) / f"{volume}.tar.gz" for volume in HUB_VOLUMES}
        assert harness.volume_inventories_seen == dict.fromkeys(HUB_VOLUMES, VOLUME_INVENTORY)


class TestManifest:
    def test_machine_identity_is_manifest_artifact_with_checksum(self, tmp_path: Path) -> None:
        gobby_home = tmp_path / ".gobby"
        backup_root = tmp_path / "backup"
        gobby_home.mkdir()
        backup_root.mkdir()
        identity = b"8fa1247f-e924-4bd7-a54e-b9dd5704304a"
        (gobby_home / "machine_id").write_bytes(identity)

        artifact = hub_cli._archive_machine_identity(gobby_home, backup_root)

        assert artifact is not None
        assert artifact.name == "machine_identity"
        assert artifact.path == "identity/machine_id"
        assert artifact.sha256 == hashlib.sha256(identity).hexdigest()
        assert (backup_root / artifact.path).read_bytes() == identity

    def test_manifest_is_written_schema_valid_and_owner_only(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        backup_root = tmp_path / "backup"
        _run_ok(runtime, backup_root)

        manifest_path = backup_root / MANIFEST_NAME
        assert manifest_path.is_file()
        assert manifest_path.stat().st_mode & 0o777 == 0o600

        manifest = load_manifest(manifest_path)
        assert manifest.manifest_format == MANIFEST_FORMAT
        assert manifest.manifest_version == MANIFEST_VERSION
        assert manifest.epoch_id is None
        assert manifest.backup_starting_head == STARTING_HEAD
        assert manifest.row_count_probes == ROW_PROBES
        assert manifest.source_identity.pg_system_identifier == SYSTEM_IDENTIFIER
        assert set(manifest.stores) == {"postgres", "qdrant", "falkordb", "volumes", "files"}
        assert len(manifest.artifacts) == NON_VOLUME_ARTIFACTS + len(HUB_VOLUMES)

    def test_manifest_includes_allow_audit_logs_with_checksums(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        logs_dir = harness.gobby_home / "logs"
        logs_dir.mkdir(parents=True)
        runtime.config.logging.dir = str(logs_dir)
        source_lines = {
            RULE_ALLOW_AUDIT_LOG_FILENAME: b'{"result":"allow"}\n',
            f"{RULE_ALLOW_AUDIT_LOG_FILENAME}.1": b'{"result":"allow","rotated":true}\n',
        }
        for filename, content in source_lines.items():
            (logs_dir / filename).write_bytes(content)

        backup_root = tmp_path / "backup"
        _run_ok(runtime, backup_root)

        manifest = load_manifest(backup_root / MANIFEST_NAME)
        records = {
            artifact.path: artifact
            for artifact in manifest.artifacts
            if artifact.path.startswith("logs/")
        }
        assert set(records) == {f"logs/{filename}" for filename in source_lines}
        for filename, content in source_lines.items():
            record = records[f"logs/{filename}"]
            assert record.sha256 == hashlib.sha256(content).hexdigest()
            assert (backup_root / record.path).read_bytes() == content

    def test_every_store_records_archive_and_restore_verification(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        backup_root = tmp_path / "backup"
        _run_ok(runtime, backup_root)

        manifest = load_manifest(backup_root / MANIFEST_NAME)
        methods = {key: store.archive_verified.method for key, store in manifest.stores.items()}
        assert methods == {
            "postgres": "pg-restore-list+sha256",
            "qdrant": "snapshot-download+sha256",
            "falkordb": "bgsave-rdb-copy+sha256",
            "volumes": "tar-archive+sha256",
            "files": "files-home-prewalk+sha256",
        }
        for store in manifest.stores.values():
            assert store.archive_verified.verified is True
            assert store.archive_verified.timestamp
            assert store.restore_verified.verified is True

    def test_manifest_carries_driver_reported_details(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        backup_root = tmp_path / "backup"
        _run_ok(runtime, backup_root)

        manifest = load_manifest(backup_root / MANIFEST_NAME)
        assert manifest.stores["postgres"].details["postgres_version"] == "16.4"
        assert manifest.stores["falkordb"].details["graphs"] == FALKORDB_GRAPHS
        assert manifest.stores["volumes"].details["volumes"] == list(HUB_VOLUMES)

    def test_manifest_never_contains_the_dsn_or_password(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        backup_root = tmp_path / "backup"
        _run_ok(runtime, backup_root)

        payload = (backup_root / MANIFEST_NAME).read_text(encoding="utf-8")
        assert DATABASE_PASSWORD not in payload
        assert "postgresql://" not in payload
        assert QDRANT_API_KEY not in payload
        assert "api_key" not in payload
        assert "password" not in payload


class TestOutputDirectory:
    def test_explicit_output_directory_is_created_owner_only(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        backup_root = tmp_path / "nested" / "backup"
        _run_ok(runtime, backup_root)

        assert backup_root.is_dir()
        assert backup_root.stat().st_mode & 0o777 == 0o700

    def test_refuses_pre_existing_output_without_touching_old_manifest(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        backup_root = tmp_path / "backup"
        backup_root.mkdir()
        old_manifest = backup_root / MANIFEST_NAME
        old_manifest.write_text("old-complete-backup\n")

        result = _invoke(runtime, "--output", str(backup_root))

        assert result.exit_code != 0
        assert str(backup_root) in result.output
        assert old_manifest.read_text() == "old-complete-backup\n"
        assert "stop_daemon" not in harness.calls

    def test_refuses_symlinked_output_root_and_names_it(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        target = tmp_path / "target"
        target.mkdir()
        backup_root = tmp_path / "backup-link"
        backup_root.symlink_to(target, target_is_directory=True)

        result = _invoke(runtime, "--output", str(backup_root))

        assert result.exit_code != 0
        assert "symlink" in result.output.lower()
        assert str(backup_root) in result.output
        assert list(target.iterdir()) == []
        assert "stop_daemon" not in harness.calls

    def test_default_output_directory_is_timestamped_under_gobby_home(
        self, harness: _Harness, runtime: CliRuntime
    ) -> None:
        result = _invoke(runtime)
        assert result.exit_code == 0, result.output

        hub_dir = harness.gobby_home / "backups" / "hub"
        created = sorted(hub_dir.iterdir())
        assert len(created) == 1
        assert re.fullmatch(r"\d{8}T\d{6}Z", created[0].name)
        assert created[0].stat().st_mode & 0o777 == 0o700
        assert hub_dir.stat().st_mode & 0o777 == 0o700
        assert (created[0] / MANIFEST_NAME).is_file()


class TestEpoch:
    def test_epoch_is_recorded_and_suppresses_the_daemon_restart(
        self,
        harness: _Harness,
        runtime: CliRuntime,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(MAINTENANCE_EPOCH_ENV, "e1")
        database = MagicMock()
        database.fetchall.side_effect = [
            [{"key": "databases.qdrant.url"}],
            [{"key": "databases.qdrant.url", "value": f'"{QDRANT_URL}"'}],
        ]
        database.fetchone.return_value = {"value": f'"{runtime.config.logging.dir}"'}
        monkeypatch.setattr(runtime, "require_database", MagicMock(return_value=database))
        backup_root = tmp_path / "backup"
        _run_ok(runtime, backup_root, "--epoch", "e1")

        manifest = load_manifest(backup_root / MANIFEST_NAME)
        assert manifest.epoch_id == "e1"
        assert "stop_daemon" in harness.calls
        assert "start_daemon" not in harness.calls

    def test_epoch_config_loading_skips_pending_destructive_migrations(
        self,
        harness: _Harness,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database = MagicMock()
        apply_migrations_values: list[bool] = []
        config = DaemonConfig()
        config.databases.qdrant.url = QDRANT_URL
        config.databases.qdrant.api_key = QDRANT_API_KEY
        config.logging.dir = str(tmp_path / "isolated-logs")
        database.fetchall.side_effect = [
            [{"key": "databases.qdrant.url"}],
            [{"key": "databases.qdrant.url", "value": f'"{QDRANT_URL}"'}],
        ]
        database.fetchone.return_value = {"value": f'"{config.logging.dir}"'}

        @contextmanager
        def open_database(
            _config_file: str | None = None,
            *,
            apply_migrations: bool = True,
        ) -> Iterator[MagicMock]:
            apply_migrations_values.append(apply_migrations)
            if apply_migrations:
                raise RuntimeError("pending destructive migration rejected")
            yield database

        class _Repository:
            def __init__(self, _db: object) -> None:
                pass

            def read(self, *, resolve_secrets: bool = True) -> Any:
                return SimpleNamespace(overrides={}, secret_bindings={})

            def runtime_candidate(
                self, _overrides: dict[str, object], _secret_bindings: object
            ) -> DaemonConfig:
                return config

        monkeypatch.setattr("gobby.cli.runtime.runtime_hub_database", open_database)
        monkeypatch.setenv(MAINTENANCE_EPOCH_ENV, "e1")
        runtime = CliRuntime(
            config_file=None,
            config_repository_factory=cast(Any, _Repository),
        )

        result = _invoke(runtime, "--output", str(tmp_path / "backup"), "--epoch", "e1")
        runtime.close()

        assert result.exit_code == 0, result.output
        assert apply_migrations_values == [False]


class TestCleanup:
    def test_daemon_is_stopped_before_any_dump_and_restarted_after_a_failure(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        harness.fail_at = "dump_postgres"

        backup_root = tmp_path / "backup"
        result = _invoke(runtime, "--output", str(backup_root))

        assert result.exit_code != 0
        assert harness.calls.index("stop_daemon") < harness.calls.index("dump_postgres")
        assert harness.calls[-1] == "start_daemon"
        assert "snapshot_qdrant" not in harness.calls
        assert not backup_root.exists()
        assert not list(tmp_path.glob(f"*/{MANIFEST_NAME}"))

    def test_corrupted_artifact_is_refused_before_manifest_publication(
        self,
        harness: _Harness,
        runtime: CliRuntime,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        original = hub_cli._run_backup

        def corrupt_after_recording(**kwargs: Any) -> object:
            manifest = original(**kwargs)
            artifact = manifest.artifacts[0]
            (kwargs["backup_root"] / artifact.path).write_bytes(b"write-time-corruption")
            return manifest

        monkeypatch.setattr(hub_cli, "_run_backup", corrupt_after_recording)
        backup_root = tmp_path / "backup"

        result = _invoke(runtime, "--output", str(backup_root))

        assert result.exit_code != 0
        assert "artifact" in result.output.lower()
        assert "sha256" in result.output.lower()
        assert not (backup_root / MANIFEST_NAME).exists()

    def test_services_are_restarted_when_tar_volumes_fails(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        harness.fail_at = "tar_volumes"

        result = _invoke(runtime, "--output", str(tmp_path / "backup"))

        assert result.exit_code != 0
        assert harness.calls.index("services_stop") < harness.calls.index("tar_volumes")
        assert harness.calls.index("tar_volumes") < harness.calls.index("services_start")
        assert "verify_postgres_restore" not in harness.calls
        assert harness.calls[-1] == "start_daemon"

    def test_failed_service_restart_after_a_clean_archive_is_fatal(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        harness.services_start_outcome = "failed"

        result = _invoke(runtime, "--output", str(tmp_path / "backup"))

        assert result.exit_code != 0
        assert "verify_postgres_restore" not in harness.calls
        assert harness.calls[-1] == "start_daemon"

    def test_epoch_service_restart_injects_pgoptions_for_postgres_healthcheck(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        services_dir = tmp_path / "services"
        services_dir.mkdir()
        (services_dir / "docker-compose.yml").write_text("services: {}\n")
        epoch = "3e553f12-2d7c-4e3f-a8c6-637e2a928942"
        pgoptions = f"-c gobby.maintenance_epoch={epoch}"
        monkeypatch.setenv(MAINTENANCE_EPOCH_ENV, epoch)
        monkeypatch.setenv("PGOPTIONS", pgoptions)

        def resolve_runtime(
            _gobby_home: Path,
            *,
            profiles: tuple[str, ...] = ("postgres", "qdrant", "falkordb"),
        ) -> ComposeRuntime:
            return ComposeRuntime(
                environment={"PGOPTIONS": pgoptions},
                profiles=profiles,
            )

        calls: list[tuple[list[str], dict[str, object]]] = []

        def run_compose(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(hub_cli, "resolve_compose_runtime", resolve_runtime, raising=False)
        monkeypatch.setattr(
            hub_cli,
            "resolve_predecessor_service_runtime",
            lambda _home, _postgres: ComposeRuntime(
                environment={"PGOPTIONS": pgoptions},
                profiles=("postgres", "qdrant", "falkordb"),
            ),
        )
        monkeypatch.setattr(subprocess, "run", run_compose)

        result = hub_cli._start_epoch_services(tmp_path)

        assert result == ServiceStartResult("success", "Docker services started")
        assert len(calls) == 2
        for command, kwargs in calls:
            assert command.count("-f") == 2
            assert command[command.index("-f", 3) + 1] == "-"
            assert kwargs["input"] == hub_cli._EPOCH_COMPOSE_OVERRIDE
            assert kwargs["env"] == {"PGOPTIONS": pgoptions}
            assert "--wait" in command

    def test_refuses_to_archive_volumes_while_services_are_still_up(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        harness.services_stop_result = False

        result = _invoke(runtime, "--output", str(tmp_path / "backup"))

        assert result.exit_code != 0
        assert "tar_volumes" not in harness.calls

    def test_daemon_is_not_restarted_when_it_was_not_running(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        harness.daemon_running = False

        _run_ok(runtime, tmp_path / "backup")

        assert "start_daemon" not in harness.calls


class TestPreflight:
    def test_missing_container_aborts_before_stopping_the_daemon(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        harness.running = ["gobby-postgres", "services-qdrant-1"]
        backup_root = tmp_path / "backup"

        result = _invoke(runtime, "--output", str(backup_root))

        assert result.exit_code != 0
        assert "services-falkordb-1" in result.output
        assert "stop_daemon" not in harness.calls
        assert harness.calls[0] == "resolve_database_url"
        assert not backup_root.exists()

    def test_the_postgres_test_container_is_not_required(self, harness: _Harness) -> None:
        assert "gobby-postgres-test-1" not in hub_cli.REQUIRED_CONTAINERS

    def test_protected_scratch_dsn_selects_only_scratch_containers_and_volumes(self) -> None:
        target = hub_cli._hub_backup_target(
            "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
        )

        assert target.containers == (
            "gobby-postgres-test-1",
            "gobby-qdrant-test-1",
            "gobby-falkordb-test-1",
        )
        assert target.falkordb_container == "gobby-falkordb-test-1"
        assert target.volumes == hub_cli.SCRATCH_HUB_VOLUMES
        assert not set(target.containers) & set(hub_cli.REQUIRED_CONTAINERS)
        assert not set(target.volumes) & set(HUB_VOLUMES)

    def test_missing_docker_cli_aborts(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        harness.docker_path = None

        result = _invoke(runtime, "--output", str(tmp_path / "backup"))

        assert result.exit_code != 0
        assert "Docker" in result.output
        assert harness.calls == ["resolve_database_url", "which:docker"]

    def test_insufficient_free_space_aborts(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        harness.free_bytes = hub_cli.MIN_FREE_BYTES - 1

        result = _invoke(runtime, "--output", str(tmp_path / "backup"))

        assert result.exit_code != 0
        assert "space" in result.output.lower()
        assert "stop_daemon" not in harness.calls


class TestJsonOutput:
    def test_json_output_stops_daemon_quietly(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        _run_ok(runtime, tmp_path / "backup", "--json")

        assert harness.stop_quiet_seen is True

    def test_json_output_reports_the_manifest_path_and_summary(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        backup_root = tmp_path / "backup"
        result = _run_ok(runtime, backup_root, "--json")

        payload = json.loads(result.output)
        assert payload["manifest"] == str(backup_root / MANIFEST_NAME)
        assert payload["backup_root"] == str(backup_root)
        assert payload["epoch_id"] is None
        assert payload["artifacts"] == NON_VOLUME_ARTIFACTS + len(HUB_VOLUMES)
        assert sorted(payload["stores"]) == ["falkordb", "files", "postgres", "qdrant", "volumes"]

    def test_json_output_never_leaks_the_dsn(
        self, harness: _Harness, runtime: CliRuntime, tmp_path: Path
    ) -> None:
        result = _run_ok(runtime, tmp_path / "backup", "--json")

        assert DATABASE_PASSWORD not in result.output
        assert "postgresql://" not in result.output
