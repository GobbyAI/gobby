"""Tests for PostgreSQL logical backup and restore helpers."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import click
import pytest

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
            return _Result((1,) if params[0] in {"pg_search", "pgaudit"} else None)
        if "gobby_migration_state" in lowered:
            return _Result(("2026-05-21T12:00:00Z",))
        raise AssertionError(f"unexpected SQL: {sql}")


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    *,
    mode: str,
) -> None:
    monkeypatch.setattr(module, "_active_install_mode", lambda **_kwargs: mode)
    monkeypatch.setattr(
        module,
        "_read_bootstrap_database_url",
        lambda _home: "postgresql://gobby:secret@localhost:60891/gobby",
    )
    monkeypatch.setattr(module.psycopg, "connect", lambda *_args, **_kwargs: _FakeConnection())


def test_create_docker_backup_writes_verified_dump_metadata_and_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(monkeypatch, backup, mode="docker")
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

    monkeypatch.setattr(backup.subprocess, "run", _run)

    result = backup.create_postgres_backup(output_dir=tmp_path / "backup", gobby_home=tmp_path)

    dump_path = tmp_path / "backup" / backup.POSTGRES_DUMP_NAME
    metadata_path = tmp_path / "backup" / backup.POSTGRES_METADATA_NAME
    sums_path = tmp_path / "backup" / backup.POSTGRES_SHA256SUMS_NAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert result["verified"] is True
    assert dump_path.read_bytes() == b"PGDMP"
    assert metadata["source_postgres_version"] == "17.6"
    assert metadata["source_dsn_redacted"] == "postgresql://gobby:****@localhost:60891/gobby"
    assert metadata["install_mode"] == "docker"
    assert metadata["pg_search_present"] is True
    assert metadata["migration_marker"]["present"] is True
    assert metadata["dump_sha256"] == hashlib.sha256(b"PGDMP").hexdigest()
    assert f"{metadata['dump_sha256']}  {backup.POSTGRES_DUMP_NAME}" in sums_path.read_text()
    assert any(command[:3] == ["docker", "exec", "gobby-postgres"] for command in commands)
    assert any(command[-2:] == ["pg_restore", "--list"] for command in commands)


def test_create_native_backup_uses_local_postgres_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(monkeypatch, backup, mode="native")
    commands: list[list[str]] = []

    def _run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        if args[0] == "pg_dump":
            dump_path = Path(args[args.index("--file") + 1])
            dump_path.write_bytes(b"LOCALDUMP")
        elif args[:2] != ["pg_restore", "--list"]:
            raise AssertionError(f"unexpected command: {args}")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(backup.subprocess, "run", _run)

    backup.create_postgres_backup(output_dir=tmp_path / "backup", gobby_home=tmp_path)

    assert commands[0][:4] == ["pg_dump", "-Fc", "--no-owner", "--no-privileges"]
    assert commands[1][:2] == ["pg_restore", "--list"]


def test_restore_docker_backup_verifies_checksum_and_runs_restore_probes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(monkeypatch, backup, mode="docker")
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

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        assert kwargs["stdin"].read() == b"PGDMP"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(backup.subprocess, "run", _run)

    result = backup.restore_postgres_backup(backup_dir, clean=True, gobby_home=tmp_path)

    assert result["verified"] is True
    assert commands[0][-2:] == ["pg_restore", "--list"]
    assert commands[1][4:8] == ["pg_restore", "--no-owner", "--no-privileges", "--clean"]
    assert "--if-exists" in commands[1]
    assert result["probes"]["pg_search_present"] is True


def test_restore_rejects_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup

    _patch_common(monkeypatch, backup, mode="native")
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / backup.POSTGRES_DUMP_NAME).write_bytes(b"PGDMP")
    (backup_dir / backup.POSTGRES_METADATA_NAME).write_text(
        json.dumps({"dump_sha256": "0" * 64}),
        encoding="utf-8",
    )

    with pytest.raises(click.ClickException, match="checksum mismatch"):
        backup.restore_postgres_backup(backup_dir, gobby_home=tmp_path)
