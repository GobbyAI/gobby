"""Tests for PostgreSQL logical backup and restore helpers."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import click
import psycopg
import pytest
import yaml

from gobby.cli import postgres_bootstrap

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _FakeConnection:
    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[str, ...] | None = None) -> _Result:
        lowered = sql.lower()
        if "server_version" in lowered:
            return _Result(("17.6",))
        if "pg_extension" in lowered:
            assert params is not None
            return _Result((1,) if params[0] in {"pg_search", "pgaudit", "pgcrypto"} else None)
        raise AssertionError(f"unexpected SQL: {sql}")


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    *,
    database_url: str = "postgresql://gobby:secret@localhost:60891/gobby",
) -> None:
    monkeypatch.setattr(
        module,
        "_read_bootstrap_database_url",
        lambda _home: database_url,
    )
    monkeypatch.setattr(module.psycopg, "connect", lambda *_args, **_kwargs: _FakeConnection())
    monkeypatch.setattr(module, "release_restored_maintenance_epoch", lambda _url: None)


def test_create_docker_backup_writes_verified_dump_metadata_and_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(monkeypatch, backup)
    commands: list[list[str]] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        if args[:3] == ["docker", "exec", "gobby-postgres"]:
            kwargs["stdout"].write(b"PGDMP")
        elif args[:4] == ["docker", "exec", "-i", "gobby-postgres"]:
            assert kwargs["stdin"].read() == b"PGDMP"
        else:
            raise AssertionError(f"unexpected command: {args}")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _run)

    result = backup.create_postgres_backup(output_dir=tmp_path / "backup", gobby_home=tmp_path)

    dump_path = tmp_path / "backup" / backup.POSTGRES_DUMP_NAME
    metadata_path = tmp_path / "backup" / backup.POSTGRES_METADATA_NAME
    sums_path = tmp_path / "backup" / backup.POSTGRES_SHA256SUMS_NAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert result["verified"] is True
    assert dump_path.read_bytes() == b"PGDMP"
    assert metadata["source_postgres_version"] == "17.6"
    assert metadata["source_dsn_redacted"] == "postgresql://gobby:****@localhost:60891/gobby"
    assert "install_mode" not in metadata
    assert metadata["pg_search_present"] is True
    assert metadata["pgcrypto_present"] is True
    assert "migration_marker" not in metadata
    assert metadata["dump_sha256"] == hashlib.sha256(b"PGDMP").hexdigest()
    assert f"{metadata['dump_sha256']}  {backup.POSTGRES_DUMP_NAME}" in sums_path.read_text()
    assert result["dump_sha256"] == metadata["dump_sha256"]
    assert result["sha256_verified"] is True
    dump_command = next(
        command for command in commands if command[:3] == ["docker", "exec", "gobby-postgres"]
    )
    assert "--no-owner" in dump_command
    assert "--no-privileges" not in dump_command
    assert any(command[-2:] == ["pg_restore", "--list"] for command in commands)


def test_create_docker_backup_uses_configured_pg_dump_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(monkeypatch, backup)
    monkeypatch.setenv("GOBBY_POSTGRES_DUMP_TIMEOUT_SECONDS", "17")
    timeouts: list[int] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if args[:3] == ["docker", "exec", "gobby-postgres"]:
            timeouts.append(kwargs["timeout"])
            kwargs["stdout"].write(b"PGDMP")
        elif args[:4] == ["docker", "exec", "-i", "gobby-postgres"]:
            assert kwargs["stdin"].read() == b"PGDMP"
        else:
            raise AssertionError(f"unexpected command: {args}")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _run)

    backup.create_postgres_backup(output_dir=tmp_path / "backup", gobby_home=tmp_path)

    assert timeouts == [17]


def test_postgres_backup_configured_only_swallows_bootstrap_read_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    monkeypatch.setattr(
        postgres_bootstrap,
        "read_bootstrap_yaml",
        lambda _path: (_ for _ in ()).throw(yaml.YAMLError("bad yaml")),
    )

    assert backup.postgres_backup_configured(gobby_home=tmp_path) is False

    monkeypatch.setattr(
        postgres_bootstrap,
        "read_bootstrap_yaml",
        lambda _path: (_ for _ in ()).throw(ValueError("unexpected")),
    )

    with pytest.raises(ValueError, match="unexpected"):
        backup.postgres_backup_configured(gobby_home=tmp_path)


def test_restore_docker_backup_verifies_checksum_and_runs_restore_probes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(
        monkeypatch,
        backup,
        database_url="postgresql://origin:secret@origin.invalid:5432/origin",
    )
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    dump_path = backup_dir / backup.POSTGRES_DUMP_NAME
    dump_path.write_bytes(b"PGDMP")
    digest = hashlib.sha256(b"PGDMP").hexdigest()
    (backup_dir / backup.POSTGRES_METADATA_NAME).write_text(
        json.dumps({"dump_sha256": digest}),
        encoding="utf-8",
    )
    (backup_dir / backup.POSTGRES_SHA256SUMS_NAME).write_text(
        f"{digest}  {backup.POSTGRES_DUMP_NAME}\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    released_targets: list[str] = []
    reset_targets: list[str] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        assert kwargs["stdin"].read() == b"PGDMP"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(
        backup,
        "_reset_postgres_database",
        lambda database_url: reset_targets.append(database_url),
    )
    monkeypatch.setattr(
        backup,
        "release_restored_maintenance_epoch",
        lambda database_url: released_targets.append(database_url),
    )

    target_database_url = "postgresql://gobby:secret@localhost:60891/gobby"
    result = backup.restore_postgres_backup(
        backup_dir,
        clean=True,
        gobby_home=tmp_path,
        database_url=target_database_url,
    )

    assert result["verified"] is True
    assert result["dump_sha256"] == digest
    assert result["sha256_verified"] is True
    assert reset_targets == [target_database_url]
    assert released_targets == [target_database_url]
    assert result["database_url"] == "postgresql://gobby:****@localhost:60891/gobby"
    assert commands[0][-2:] == ["pg_restore", "--list"]
    assert commands[1][3:6] == ["-e", "PGOPTIONS=-c event_triggers=off", "gobby-postgres"]
    assert commands[1][6:8] == ["pg_restore", "--no-owner"]
    assert "--no-privileges" not in commands[1]
    assert "--clean" not in commands[1]
    assert result["probes"]["pg_search_present"] is True
    assert result["probes"]["pgcrypto_present"] is True


def test_pg_restore_targets_protected_test_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    dump_path = tmp_path / backup.POSTGRES_DUMP_NAME
    dump_path.write_bytes(b"PGDMP")
    commands: list[list[str]] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        assert kwargs["stdin"].read() == b"PGDMP"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _run)

    backup._run_pg_restore(
        database_url="postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test",
        dump_path=dump_path,
        clean=True,
    )

    assert commands[0][5] == "gobby-postgres-test-1"
    assert commands[0][-4:] == ["-U", "gobby_test", "-d", "gobby_test"]


def test_pg_restore_rejects_test_container_without_test_protection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    dump_path = tmp_path / backup.POSTGRES_DUMP_NAME
    dump_path.write_bytes(b"PGDMP")
    monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

    with pytest.raises(click.ClickException, match="GOBBY_TEST_PROTECT=1"):
        backup._run_pg_restore(
            database_url="postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test",
            dump_path=dump_path,
            clean=True,
        )


def test_clean_restore_recreates_exact_target_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.cli.postgres_backup as backup

    statements: list[str] = []
    connection_args: list[tuple[str, dict[str, object]]] = []

    class _AdminConnection:
        def __enter__(self) -> _AdminConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: object) -> None:
            statements.append(repr(query))

    def _connect(dsn: str, **kwargs: object) -> _AdminConnection:
        connection_args.append((dsn, kwargs))
        return _AdminConnection()

    monkeypatch.setattr(psycopg, "connect", _connect)

    backup._reset_postgres_database("postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test")

    assert "dbname=postgres" in connection_args[0][0]
    assert connection_args[0][1] == {"autocommit": True, "connect_timeout": 10}
    assert "DROP DATABASE IF EXISTS" in statements[0]
    assert "Identifier('gobby_test')" in statements[0]
    assert "CREATE DATABASE" in statements[1]
    assert statements[1].count("Identifier('gobby_test')") == 2


def test_restore_rejects_unverified_dump_without_sidecar_before_pg_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(monkeypatch, backup)
    dump_path = tmp_path / backup.POSTGRES_DUMP_NAME
    dump_path.write_bytes(b"PGDMP")

    def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("pg_restore must not run for an unverified dump")

    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(click.ClickException, match="missing trusted checksum sidecar"):
        backup.restore_postgres_backup(dump_path, gobby_home=tmp_path)


def test_restore_allows_explicit_unverified_dump_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(monkeypatch, backup)
    dump_path = tmp_path / backup.POSTGRES_DUMP_NAME
    dump_path.write_bytes(b"PGDMP")
    commands: list[list[str]] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        assert kwargs["stdin"].read() == b"PGDMP"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _run)

    result = backup.restore_postgres_backup(
        dump_path,
        allow_unverified=True,
        gobby_home=tmp_path,
    )

    assert result["sha256_verified"] is False
    assert result["expected_dump_sha256"] is None
    assert commands[0][-2:] == ["pg_restore", "--list"]
    assert commands[1][6:8] == ["pg_restore", "--no-owner"]
    assert "--no-privileges" not in commands[1]


def test_restore_rejects_unmanaged_dsn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(
        monkeypatch,
        backup,
        database_url="postgresql://gobby:secret@db.example.test:5432/gobby",
    )

    with pytest.raises(click.ClickException) as exc_info:
        backup.restore_postgres_backup(tmp_path / "missing", gobby_home=tmp_path)

    message = str(exc_info.value)
    assert "host=db.example.test" in message
    assert "port=5432" in message
    assert "user=gobby" in message
    assert "database=gobby" in message


def test_restore_rejects_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(monkeypatch, backup)
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / backup.POSTGRES_DUMP_NAME).write_bytes(b"PGDMP")
    (backup_dir / backup.POSTGRES_METADATA_NAME).write_text(
        json.dumps({"dump_sha256": "0" * 64}),
        encoding="utf-8",
    )

    with pytest.raises(click.ClickException, match="checksum mismatch"):
        backup.restore_postgres_backup(backup_dir, gobby_home=tmp_path)
