"""Tests for the hub-backup per-store backup drivers."""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import tarfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import ANY

import click
import httpx
import psycopg
import pytest

from gobby.cli.hub_backup import _stores as stores
from gobby.storage.maintenance_epoch import MAINTENANCE_EPOCH_ENV

pytestmark = pytest.mark.unit

DATABASE_URL = "postgresql://gobby:secret@localhost:60891/gobby"
TEST_DATABASE_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
SYSTEM_IDENTIFIER = "7412345678901234567"
DATABASE_OID = 16401


# --------------------------------------------------------------------------
# psycopg fakes
# --------------------------------------------------------------------------


def _table_from_count_sql(sql: str) -> str:
    quoted = sql.split("public.", 1)[1].strip()
    return quoted.strip('"').replace('""', '"')


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class _FakeConnection:
    """Fake psycopg connection dispatching on SQL substrings."""

    def __init__(
        self,
        *,
        tables: Sequence[str] = (),
        counts: dict[str, int] | None = None,
        head: object = 41,
        roles: Sequence[tuple[str, bool, bool]] = (),
        schema_objects: Sequence[tuple[str, int]] = (),
    ) -> None:
        self.tables = list(tables)
        self.counts = dict(counts or {})
        self.head = head
        self.roles = list(roles)
        self.schema_objects = list(schema_objects)
        self.statements: list[str] = []

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: tuple[object, ...] | None = None) -> _FakeCursor:
        self.statements.append(sql)
        lowered = sql.lower()
        if "pg_control_system" in lowered:
            return _FakeCursor([(SYSTEM_IDENTIFIER,)])
        if "from pg_database" in lowered:
            return _FakeCursor([(DATABASE_OID,)])
        if "schema_migrations" in lowered:
            return _FakeCursor([(self.head,)])
        if "from pg_tables" in lowered:
            return _FakeCursor([(name,) for name in self.tables])
        if "drain_ephemeral_principals" in lowered:
            return _FakeCursor([(0,)])
        if "from pg_roles" in lowered and "rolname ~" in lowered:
            return _FakeCursor(
                [
                    tuple(role)
                    for role in self.roles
                    if re.fullmatch(r"gobby_agent_[0-9a-f]{32}_[1-9][0-9]*", str(role[0]))
                    and bool(role[2])
                ]
            )
        if "from pg_roles" in lowered:
            return _FakeCursor([tuple(role) for role in self.roles])
        if "from pg_class" in lowered:
            return _FakeCursor([tuple(row) for row in self.schema_objects])
        if "server_version" in lowered:
            return _FakeCursor([("17.6",)])
        if lowered.startswith("select count(*)"):
            return _FakeCursor([(self.counts[_table_from_count_sql(sql)],)])
        if "current_database()" in lowered:
            return _FakeCursor([("gobby",)])
        if "current_schema()" in lowered:
            return _FakeCursor([("public",)])
        raise AssertionError(f"unexpected SQL: {sql}")


def _patch_psycopg(
    monkeypatch: pytest.MonkeyPatch,
    connection: _FakeConnection,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def _connect(dsn: str, **kwargs: object) -> _FakeConnection:
        calls.append({"dsn": dsn, **kwargs})
        return connection

    monkeypatch.setattr(psycopg, "connect", _connect)
    return calls


def _completed(
    args: list[str],
    *,
    returncode: int = 0,
    stdout: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=b"" if returncode == 0 else b"container exploded",
    )


# --------------------------------------------------------------------------
# collect_postgres_identity / probes / roles
# --------------------------------------------------------------------------


def test_collect_postgres_identity_returns_identity_and_migration_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(head=41)
    calls = _patch_psycopg(monkeypatch, connection)

    identity, head = stores.collect_postgres_identity(DATABASE_URL)

    assert identity.pg_system_identifier == SYSTEM_IDENTIFIER
    assert identity.database_name == "gobby"
    assert identity.database_oid == DATABASE_OID
    assert head == 41
    assert calls == [{"dsn": DATABASE_URL, "connect_timeout": 10}]
    assert any("pg_control_system()" in statement for statement in connection.statements)


def test_collect_postgres_identity_rejects_empty_schema_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_psycopg(monkeypatch, _FakeConnection(head=None))

    with pytest.raises(click.ClickException) as excinfo:
        stores.collect_postgres_identity(DATABASE_URL)

    assert "schema_migrations" in str(excinfo.value)


def test_collect_row_count_probes_quotes_identifiers_and_counts_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        tables=['ta"ble', "tasks"],
        counts={'ta"ble': 2, "tasks": 7},
    )
    _patch_psycopg(monkeypatch, connection)

    probes = stores.collect_row_count_probes(DATABASE_URL)

    assert probes == {'ta"ble': 2, "tasks": 7}
    count_statements = [s for s in connection.statements if s.lower().startswith("select count(*)")]
    assert count_statements == [
        'SELECT count(*) FROM public."ta""ble"',
        'SELECT count(*) FROM public."tasks"',
    ]
    listing = next(s for s in connection.statements if "pg_tables" in s)
    assert "'public'" in listing
    assert "ORDER BY tablename" in listing


def test_collect_schema_object_counts_groups_public_schema_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(schema_objects=[("index", 14), ("table", 7), ("view", 2)])
    _patch_psycopg(monkeypatch, connection)

    result = stores.collect_schema_object_counts(DATABASE_URL)

    assert result == {"index": 14, "table": 7, "view": 2}
    assert any("from pg_class" in sql.lower() for sql in connection.statements)


def test_collect_source_roles_skips_builtin_roles_and_reports_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        roles=[
            ("gobby", True, True),
            ("gobby_ro", False, True),
            ("gobby_agent_issuer", False, False),
            ("gobby_daemon_runtime", False, True),
            ("gobby_gcode_capability", False, False),
            ("gobby_agent_0123456789abcdef0123456789abcdef_1", False, True),
            ("gobby_ix_e94cf5ac3163ddb1_1", False, True),
            ("gobby_ix_claude_01234567_89abcdef_7", False, True),
            ("gobby_mnt_0123456789abcdef0123456789abcdef_2", False, True),
        ]
    )
    _patch_psycopg(monkeypatch, connection)

    roles = stores.collect_source_roles(DATABASE_URL)

    assert roles == [
        {"rolname": "gobby", "rolsuper": True, "rolcanlogin": True},
        {"rolname": "gobby_ro", "rolsuper": False, "rolcanlogin": True},
        {"rolname": "gobby_agent_issuer", "rolsuper": False, "rolcanlogin": False},
        {"rolname": "gobby_daemon_runtime", "rolsuper": False, "rolcanlogin": True},
        {"rolname": "gobby_gcode_capability", "rolsuper": False, "rolcanlogin": False},
    ]
    statement = next(s for s in connection.statements if "pg_roles" in s)
    assert "NOT LIKE 'pg\\_%'" in statement
    assert (
        "!~ '^(gobby_agent_[0-9a-f]{32}"
        "|gobby_ix_([0-9a-f]{16}|[A-Za-z0-9]{1,8}_[0-9a-f]{8}_[0-9a-f]{8})"
        "|gobby_mnt_[0-9a-f]{32})_[1-9][0-9]*$'"
    ) in statement
    assert "ORDER BY rolname" in statement


# --------------------------------------------------------------------------
# dump_postgres
# --------------------------------------------------------------------------


def _postgres_runner(
    commands: list[list[str]],
    *,
    dump_payload: bytes = b"PGDMP",
    globals_payload: bytes = b"CREATE ROLE gobby;\n",
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(list(args))
        if "pg_dump" in args:
            kwargs["stdout"].write(dump_payload)
        elif "pg_dumpall" in args:
            kwargs["stdout"].write(globals_payload)
        elif "pg_restore" in args:
            assert kwargs["stdin"].read() == dump_payload
        else:
            raise AssertionError(f"unexpected command: {args}")
        return _completed(args)

    return _run


def test_dump_postgres_writes_dump_and_globals_with_checksums(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_psycopg(monkeypatch, _FakeConnection())
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _postgres_runner(commands))

    artifacts, details = stores.dump_postgres(DATABASE_URL, tmp_path)

    dump_path = tmp_path / stores.POSTGRES_DUMP_RELPATH
    globals_path = tmp_path / stores.GLOBALS_DUMP_RELPATH
    assert dump_path.read_bytes() == b"PGDMP"
    assert globals_path.read_bytes() == b"CREATE ROLE gobby;\n"
    assert [artifact.path for artifact in artifacts] == [
        "postgres/gobby.dump",
        "postgres/globals.sql",
    ]
    assert artifacts[0].sha256 == hashlib.sha256(b"PGDMP").hexdigest()
    assert artifacts[0].size_bytes == len(b"PGDMP")
    assert artifacts[1].sha256 == hashlib.sha256(b"CREATE ROLE gobby;\n").hexdigest()
    assert details == {"postgres_version": "17.6", "archive_list_checked": True}
    assert dump_path.stat().st_mode & 0o777 == 0o600
    assert globals_path.stat().st_mode & 0o777 == 0o600
    assert dump_path.parent.stat().st_mode & 0o777 == 0o700
    assert ["docker", "exec", "-i", "gobby-postgres", "pg_restore", "--list"] in commands


def test_dump_postgres_preserves_ownership_and_privileges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_psycopg(monkeypatch, _FakeConnection())
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _postgres_runner(commands))

    stores.dump_postgres(DATABASE_URL, tmp_path)

    dump_command = next(command for command in commands if "pg_dump" in command)
    assert dump_command == [
        "docker",
        "exec",
        "gobby-postgres",
        "pg_dump",
        "-U",
        "gobby",
        "-d",
        "gobby",
        "-Fc",
    ]
    assert "--no-owner" not in dump_command
    assert "--no-privileges" not in dump_command


def test_dump_postgres_captures_cluster_globals_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_psycopg(monkeypatch, _FakeConnection())
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _postgres_runner(commands))

    stores.dump_postgres(DATABASE_URL, tmp_path)

    globals_command = next(command for command in commands if "pg_dumpall" in command)
    assert globals_command == [
        "docker",
        "exec",
        "gobby-postgres",
        "pg_dumpall",
        "-U",
        "gobby",
        "--globals-only",
    ]


def test_dump_postgres_routes_every_client_to_protected_test_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_psycopg(monkeypatch, _FakeConnection())
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _postgres_runner(commands))
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

    stores.dump_postgres(TEST_DATABASE_URL, tmp_path)

    assert len(commands) == 3
    assert all("gobby-postgres-test-1" in command for command in commands)
    assert all("gobby-postgres" not in command for command in commands)


def test_dump_postgres_aborts_while_ephemeral_login_remains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _FakeConnection(
        roles=[("gobby_agent_0123456789abcdef0123456789abcdef_1", False, True)]
    )
    _patch_psycopg(monkeypatch, connection)
    monkeypatch.setattr(subprocess, "run", _postgres_runner([]))

    with pytest.raises(click.ClickException, match="ephemeral PostgreSQL login remains"):
        stores.dump_postgres(DATABASE_URL, tmp_path)


def test_restore_postgres_globals_targets_protected_test_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    globals_path = tmp_path / "globals.sql"
    globals_path.write_bytes(
        b"CREATE ROLE gobby;\n"
        b"ALTER ROLE gobby WITH LOGIN;\n"
        b"GRANT gobby_runtime TO gobby GRANTED BY gobby;\n"
    )
    calls: list[tuple[list[str], bytes]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((list(args), kwargs["input"]))
        return _completed(args)

    monkeypatch.setattr(subprocess, "run", run)

    stores.restore_postgres_globals(TEST_DATABASE_URL, globals_path)

    assert calls[0][0] == [
        "docker",
        "exec",
        "-i",
        "gobby-postgres-test-1",
        "psql",
        "-U",
        "gobby_test",
        "-d",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
    ]
    replay = calls[0][1]
    assert b"EXCEPTION WHEN duplicate_object THEN" in replay
    assert b"CREATE ROLE gobby;" in replay
    assert b"ALTER ROLE gobby WITH LOGIN;" in replay
    assert b"GRANT gobby_runtime TO gobby;" in replay
    assert b"GRANTED BY" not in replay


def test_dump_postgres_forwards_maintenance_pgoptions_into_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_psycopg(monkeypatch, _FakeConnection())
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _postgres_runner(commands))
    monkeypatch.setenv(
        MAINTENANCE_EPOCH_ENV,
        "28fc4ff4-8454-4ed9-b4db-a0adcb1ca674",
    )
    monkeypatch.setenv(
        "PGOPTIONS",
        "-c gobby.maintenance_epoch=28fc4ff4-8454-4ed9-b4db-a0adcb1ca674",
    )

    stores.dump_postgres(DATABASE_URL, tmp_path)

    live_commands = [
        command for command in commands if "pg_dump" in command or "pg_dumpall" in command
    ]
    assert len(live_commands) == 2
    assert all(command[2:4] == ["-e", "PGOPTIONS"] for command in live_commands)


def test_dump_postgres_raises_when_pg_dump_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_psycopg(monkeypatch, _FakeConnection())

    def _run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return _completed(args, returncode=1)

    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(click.ClickException) as excinfo:
        stores.dump_postgres(DATABASE_URL, tmp_path)

    assert "container exploded" in str(excinfo.value)


# --------------------------------------------------------------------------
# snapshot_qdrant
# --------------------------------------------------------------------------


class _FakeNamed:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCollections:
    def __init__(self, names: Sequence[str]) -> None:
        self.collections = [_FakeNamed(name) for name in names]


class _FakeCount:
    def __init__(self, count: int) -> None:
        self.count = count


class _FakeQdrantClient:
    def __init__(self, *, url: str, api_key: str | None = None, timeout: int | None = None) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self.points = {"memories": 12, "notes": 3}
        self.records: dict[str, list[dict[str, object]]] = {
            "memories": [{"id": 1, "payload": {"kind": "memory"}, "vector": [1.0]}],
            "notes": [{"id": 2, "payload": {"kind": "note"}, "vector": [2.0]}],
        }
        self.created: list[str] = []
        self.deleted: list[tuple[str, str]] = []

    def get_collections(self) -> _FakeCollections:
        return _FakeCollections(["notes", "memories"])

    def count(self, collection_name: str, exact: bool = False) -> _FakeCount:
        assert exact is True
        return _FakeCount(self.points[collection_name])

    def scroll(
        self,
        collection_name: str,
        *,
        limit: int,
        offset: object = None,
        with_payload: bool,
        with_vectors: bool,
    ) -> tuple[list[dict[str, object]], None]:
        assert limit > 0
        assert offset is None
        assert with_payload is True
        assert with_vectors is True
        return self.records[collection_name], None

    def create_snapshot(self, collection_name: str, wait: bool = False) -> _FakeNamed:
        assert wait is True
        self.created.append(collection_name)
        return _FakeNamed(f"{collection_name}-2026.snapshot")

    def delete_snapshot(
        self,
        collection_name: str,
        snapshot_name: str,
        wait: bool = False,
    ) -> bool:
        assert wait is True
        self.deleted.append((collection_name, snapshot_name))
        return True


class _FakeStreamResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self) -> Iterator[bytes]:
        yield self._payload[: len(self._payload) // 2]
        yield self._payload[len(self._payload) // 2 :]


class _FakeStream:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._payload)

    def __exit__(self, *_args: object) -> None:
        return None


def _patch_qdrant(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[_FakeQdrantClient], list[dict[str, object]]]:
    clients: list[_FakeQdrantClient] = []
    requests: list[dict[str, object]] = []

    def _client(
        url: str,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> _FakeQdrantClient:
        client = _FakeQdrantClient(url=url, api_key=api_key, timeout=timeout)
        clients.append(client)
        return client

    def _stream(method: str, url: str, **kwargs: Any) -> _FakeStream:
        requests.append({"method": method, "url": url, "headers": kwargs.get("headers")})
        return _FakeStream(f"snapshot-of-{url.rsplit('/', 3)[1]}".encode())

    monkeypatch.setattr(stores, "QdrantClient", _client)
    monkeypatch.setattr(httpx, "stream", _stream)
    return clients, requests


def test_snapshot_qdrant_downloads_then_deletes_each_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clients, requests = _patch_qdrant(monkeypatch)

    artifacts, details = stores.snapshot_qdrant("http://127.0.0.1:6333", None, tmp_path)

    memories = tmp_path / "qdrant" / "memories.snapshot"
    assert memories.read_bytes() == b"snapshot-of-memories"
    assert memories.stat().st_mode & 0o777 == 0o600
    assert memories.parent.stat().st_mode & 0o777 == 0o700
    assert [artifact.path for artifact in artifacts] == [
        "qdrant/memories.snapshot",
        "qdrant/notes.snapshot",
    ]
    assert artifacts[0].sha256 == hashlib.sha256(b"snapshot-of-memories").hexdigest()
    assert artifacts[0].size_bytes == len(b"snapshot-of-memories")
    assert details == {
        "collections": {
            "memories": {
                "points": 12,
                "snapshot": "qdrant/memories.snapshot",
                "content_sha256": ANY,
            },
            "notes": {
                "points": 3,
                "snapshot": "qdrant/notes.snapshot",
                "content_sha256": ANY,
            },
        }
    }
    assert clients[0].deleted == [
        ("memories", "memories-2026.snapshot"),
        ("notes", "notes-2026.snapshot"),
    ]
    assert requests[0]["url"] == (
        "http://127.0.0.1:6333/collections/memories/snapshots/memories-2026.snapshot"
    )
    assert requests[0]["headers"] == {}


def test_snapshot_qdrant_sends_api_key_header_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clients, requests = _patch_qdrant(monkeypatch)

    stores.snapshot_qdrant("http://127.0.0.1:6333", "s3cret", tmp_path)

    assert clients[0].api_key == "s3cret"
    assert all(request["headers"] == {"api-key": "s3cret"} for request in requests)


def test_snapshot_qdrant_refuses_when_no_server_url_is_configured(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException) as excinfo:
        stores.snapshot_qdrant(None, None, tmp_path)

    assert "snapshot" in str(excinfo.value).lower()
    assert not (tmp_path / "qdrant").exists()


# --------------------------------------------------------------------------
# dump_falkordb
# --------------------------------------------------------------------------


class _FalkorRunner:
    def __init__(
        self,
        *,
        lastsave: Sequence[int],
        persistence: Sequence[str],
        graphs: str = "social\nmemory\n",
        dbsize: str = "4",
        graph_counts: dict[str, dict[str, int]] | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self._lastsave = [str(value) for value in lastsave]
        self._persistence = list(persistence)
        self._graphs = graphs
        self._dbsize = dbsize
        self._graph_counts = graph_counts or {
            "memory": {"nodes": 5, "edges": 3},
            "social": {"nodes": 9, "edges": 7},
        }

    def _pop(self, values: list[str]) -> str:
        """Return the next scripted reply, repeating the final one forever."""
        return values.pop(0) if len(values) > 1 else values[0]

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(list(args))
        if args[:2] == ["docker", "cp"]:
            Path(args[3]).write_bytes(b"REDIS0011-RDB")
            return _completed(args)
        request = args[-1]
        if request.endswith("LASTSAVE"):
            return _completed(args, stdout=self._pop(self._lastsave).encode() + b"\n")
        if request.endswith("INFO persistence"):
            return _completed(args, stdout=self._pop(self._persistence).encode())
        if request.endswith("BGSAVE"):
            return _completed(args, stdout=b"Background saving started\n")
        if request.endswith("GRAPH.LIST"):
            return _completed(args, stdout=self._graphs.encode())
        if "GRAPH.QUERY" in request:
            graph = next(name for name in self._graph_counts if f"GRAPH.QUERY {name} " in request)
            metric = "nodes" if "count(n)" in request else "edges"
            return _completed(args, stdout=f"{self._graph_counts[graph][metric]}\n".encode())
        if request.endswith("DBSIZE"):
            return _completed(args, stdout=self._dbsize.encode() + b"\n")
        raise AssertionError(f"unexpected command: {args}")


_BGSAVE_RUNNING = "rdb_bgsave_in_progress:1\r\nrdb_last_bgsave_status:ok\r\n"
_BGSAVE_DONE = "rdb_bgsave_in_progress:0\r\nrdb_last_bgsave_status:ok\r\n"
_BGSAVE_FAILED = "rdb_bgsave_in_progress:0\r\nrdb_last_bgsave_status:err\r\n"


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    pauses: list[float] = []
    monkeypatch.setattr(time, "sleep", pauses.append)
    return pauses


def test_dump_falkordb_polls_bgsave_to_completion_and_copies_rdb(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _FalkorRunner(
        lastsave=[100, 100, 200],
        persistence=[_BGSAVE_RUNNING, _BGSAVE_DONE],
    )
    monkeypatch.setattr(subprocess, "run", runner)
    pauses = _patch_sleep(monkeypatch)

    artifacts, details = stores.dump_falkordb(tmp_path)

    rdb_path = tmp_path / "falkordb" / "dump.rdb"
    assert rdb_path.read_bytes() == b"REDIS0011-RDB"
    assert rdb_path.stat().st_mode & 0o777 == 0o600
    assert rdb_path.parent.stat().st_mode & 0o777 == 0o700
    assert [artifact.path for artifact in artifacts] == ["falkordb/dump.rdb"]
    assert artifacts[0].sha256 == hashlib.sha256(b"REDIS0011-RDB").hexdigest()
    assert artifacts[0].size_bytes == len(b"REDIS0011-RDB")
    assert details == {
        "graphs": ["memory", "social"],
        "graph_inventory": {
            "memory": {"nodes": 5, "edges": 3},
            "social": {"nodes": 9, "edges": 7},
        },
        "dbsize": 4,
    }
    assert pauses, "poll loop should wait between INFO persistence probes"
    info_polls = [c for c in runner.commands if c[-1].endswith("INFO persistence")]
    assert len(info_polls) == 2
    assert runner.commands[-1] == [
        "docker",
        "cp",
        "services-falkordb-1:/var/lib/falkordb/data/dump.rdb",
        str(rdb_path),
    ]


def test_dump_falkordb_authenticates_with_in_container_password_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _FalkorRunner(lastsave=[100, 200], persistence=[_BGSAVE_DONE])
    monkeypatch.setattr(subprocess, "run", runner)
    _patch_sleep(monkeypatch)

    stores.dump_falkordb(tmp_path)

    bgsave = next(c for c in runner.commands if c[-1].endswith("BGSAVE"))
    assert bgsave[:5] == ["docker", "exec", "services-falkordb-1", "sh", "-c"]
    assert bgsave[5] == ('redis-cli -a "$GOBBY_FALKORDB_PASSWORD" --no-auth-warning --raw BGSAVE')
    assert all("secret" not in " ".join(command) for command in runner.commands)


def test_dump_falkordb_routes_every_command_to_selected_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _FalkorRunner(lastsave=[100, 200], persistence=[_BGSAVE_DONE])
    monkeypatch.setattr(subprocess, "run", runner)
    _patch_sleep(monkeypatch)

    stores.dump_falkordb(tmp_path, container="gobby-falkordb-test-1")

    assert all(
        command[2].startswith("gobby-falkordb-test-1")
        for command in runner.commands
        if command[:2] in (["docker", "exec"], ["docker", "cp"])
    )


def test_dump_falkordb_rejects_failed_bgsave_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _FalkorRunner(lastsave=[100, 200], persistence=[_BGSAVE_FAILED])
    monkeypatch.setattr(subprocess, "run", runner)
    _patch_sleep(monkeypatch)

    with pytest.raises(click.ClickException) as excinfo:
        stores.dump_falkordb(tmp_path)

    assert "err" in str(excinfo.value)
    assert not (tmp_path / "falkordb" / "dump.rdb").exists()


def test_dump_falkordb_times_out_when_bgsave_never_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _FalkorRunner(lastsave=[100], persistence=[_BGSAVE_RUNNING])
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr(stores, "FALKORDB_BGSAVE_TIMEOUT_SECONDS", 0)
    _patch_sleep(monkeypatch)

    with pytest.raises(click.ClickException) as excinfo:
        stores.dump_falkordb(tmp_path)

    assert "BGSAVE" in str(excinfo.value)


# --------------------------------------------------------------------------
# tar_volumes
# --------------------------------------------------------------------------


def _archive_destination(args: list[str]) -> Path:
    mount = next(value for value in args if value.endswith(":/backup"))
    target = next(value for value in args if value.startswith("/backup/"))
    return Path(mount.rsplit(":/backup", 1)[0]) / Path(target).name


def _volume_runner(
    commands: list[list[str]],
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    def _run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(list(args))
        destination = _archive_destination(args)
        destination.write_bytes(b"tar-of-" + destination.name.encode())
        return _completed(args)

    return _run


def _patch_source_volume_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stores,
        "_source_volume_inventory",
        lambda _volume: {"members": 2, "sha256": "b" * 64},
    )


def test_source_volume_inventory_hashes_regular_file_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"source-volume-content"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo("data/value.bin")
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))
    commands: list[list[str]] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        kwargs["stdout"].write(archive.getvalue())
        return _completed(args)

    monkeypatch.setattr(subprocess, "run", _run)

    inventory = stores._source_volume_inventory("gobby_qdrant_data")

    assert inventory["members"] == 1
    assert isinstance(inventory["sha256"], str)
    assert commands == [
        [
            "docker",
            "run",
            "--rm",
            "-v",
            "gobby_qdrant_data:/source:ro",
            "alpine",
            "tar",
            "cf",
            "-",
            "-C",
            "/source",
            ".",
        ]
    ]


def test_tar_volumes_archives_each_volume_via_docker_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _volume_runner(commands))
    _patch_source_volume_inventory(monkeypatch)

    artifacts, details = stores.tar_volumes(tmp_path, ["gobby_qdrant_data"])

    archive = tmp_path / "volumes" / "gobby_qdrant_data.tar.gz"
    assert commands == [
        [
            "docker",
            "run",
            "--rm",
            "-v",
            "gobby_qdrant_data:/source:ro",
            "-v",
            f"{archive.parent}:/backup",
            "alpine",
            "tar",
            "czf",
            "/backup/gobby_qdrant_data.tar.gz",
            "-C",
            "/source",
            ".",
        ]
    ]
    assert [artifact.path for artifact in artifacts] == ["volumes/gobby_qdrant_data.tar.gz"]
    assert artifacts[0].sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert artifacts[0].size_bytes == archive.stat().st_size
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.parent.stat().st_mode & 0o777 == 0o700
    assert details == {
        "volumes": ["gobby_qdrant_data"],
        "source_inventories": {"gobby_qdrant_data": {"members": 2, "sha256": "b" * 64}},
    }


def test_tar_volumes_uses_generous_timeout_for_multi_gigabyte_volumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    timeouts: list[float] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        timeouts.append(kwargs["timeout"])
        _archive_destination(args).write_bytes(b"x")
        return _completed(args)

    monkeypatch.setattr(subprocess, "run", _run)
    _patch_source_volume_inventory(monkeypatch)

    stores.tar_volumes(tmp_path, ["gobby_postgres_data"])

    assert timeouts and all(timeout >= 600 for timeout in timeouts)


def test_tar_volumes_defaults_to_every_hub_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _volume_runner(commands))
    _patch_source_volume_inventory(monkeypatch)

    artifacts, details = stores.tar_volumes(tmp_path)

    assert stores.HUB_VOLUMES == (
        "gobby_postgres_data",
        "gobby_qdrant_data",
        "gobby_falkordb_data",
    )
    assert details == {
        "volumes": list(stores.HUB_VOLUMES),
        "source_inventories": {
            volume: {"members": 2, "sha256": "b" * 64} for volume in stores.HUB_VOLUMES
        },
    }
    assert [artifact.path for artifact in artifacts] == [
        f"volumes/{volume}.tar.gz" for volume in stores.HUB_VOLUMES
    ]
    assert [command[4] for command in commands] == [
        f"{volume}:/source:ro" for volume in stores.HUB_VOLUMES
    ]


def test_tar_volumes_raises_when_docker_run_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return _completed(args, returncode=2)

    monkeypatch.setattr(subprocess, "run", _run)
    _patch_source_volume_inventory(monkeypatch)

    with pytest.raises(click.ClickException) as excinfo:
        stores.tar_volumes(tmp_path, ["gobby_qdrant_data"])

    assert "gobby_qdrant_data" in str(excinfo.value)


def test_artifact_destination_refuses_symlinked_leaf(tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    artifact_dir = backup_root / "postgres"
    artifact_dir.mkdir(parents=True)
    outside = tmp_path / "outside.dump"
    outside.write_bytes(b"must-remain-untouched")
    destination = artifact_dir / "gobby.dump"
    destination.symlink_to(outside)

    with pytest.raises(click.ClickException) as excinfo:
        stores._prepare_artifact_path(backup_root, "postgres/gobby.dump")

    assert "symlink" in str(excinfo.value).lower()
    assert str(destination) in str(excinfo.value)
    assert outside.read_bytes() == b"must-remain-untouched"


def test_audit_log_source_refuses_symlinked_parent(tmp_path: Path) -> None:
    real_logs = tmp_path / "real-logs"
    real_logs.mkdir()
    (real_logs / "rule-allow-audit.jsonl").write_text('{"result":"allow"}\n')
    logs_dir = tmp_path / "logs-link"
    logs_dir.symlink_to(real_logs, target_is_directory=True)

    with pytest.raises(click.ClickException) as excinfo:
        stores.archive_rule_allow_audit_logs(logs_dir, tmp_path / "backup")

    assert "symlink" in str(excinfo.value).lower()
    assert str(logs_dir) in str(excinfo.value)
