"""Tests for hub-backup scratch-restore verification.

`restore_verified` is earned only by real scratch restores, so these tests pin the
exact docker/psql/redis-cli surface each verifier drives. No real docker is used.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import IO, Any

import click
import psycopg
import pytest

from gobby.cli.hub_backup import _stores as stores
from gobby.cli.hub_backup import _verify
from gobby.cli.hub_backup import cli as hub_cli
from gobby.cli.hub_backup._content import archive_inventory
from gobby.cli.hub_backup._verify import RoleExpectation

pytestmark = pytest.mark.unit


def _point_digest(points: list[dict[str, object]]) -> str:
    records = sorted(
        hashlib.sha256(
            json.dumps(
                {key: point.get(key) for key in ("id", "payload", "vector")},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).digest()
        for point in points
    )
    digest = hashlib.sha256()
    for record in records:
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
    return digest.hexdigest()


class _DockerFake:
    """Strict recording fake for `subprocess.run` over fixed docker argv."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.stdin_payloads: dict[str, bytes] = {}
        self.pg_ready_after: int = 1
        self.falkor_ready_after: int = 1
        self.role_rows: list[tuple[str, bool, bool]] = []
        self.table_counts: dict[str, int] = {}
        self.schema_object_counts: dict[str, int] = {}
        self.graphs: list[str] = []
        self.graph_counts: dict[str, dict[str, int]] = {}
        self.globals_returncode: int = 3
        self.globals_stderr: bytes = b'ERROR:  role "postgres" already exists'
        self.pg_restore_returncode: int = 0
        self.pg_restore_stderr: bytes = b""
        self.container_id = "c0ffee1234567890"
        self.inspect_id: str | None = None
        self.inspect_drop_labels: set[str] = set()
        self.inspect_extra_labels: dict[str, str] = {}
        self.run_labels: dict[str, str] = {}
        self.mount_source: Path | None = None
        self.mounted_rdb: bytes | None = None
        self._pg_ready_seen = 0
        self._falkor_ready_seen = 0

    # -- dispatch ---------------------------------------------------------
    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(args))
        stdin: IO[bytes] | None = kwargs.get("stdin")
        assert kwargs.get("timeout"), f"missing subprocess timeout: {args}"
        assert args[0] == "docker", f"unexpected program: {args}"
        if args[1] == "run":
            return self._handle_run(args)
        if args[1:3] == ["container", "inspect"]:
            return self._handle_inspect(args)
        if args[1:3] == ["rm", "-f"]:
            return self._done(args)
        if args[1] == "exec":
            rest = args[2:]
            piped = rest[0] == "-i"
            if piped:
                rest = rest[1:]
            if rest[0] == "-e":
                rest = rest[2:]
            return self._handle_exec(args, rest[1:], piped=piped, stdin=stdin)
        raise AssertionError(f"unexpected command: {args}")

    def _handle_run(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        for index, arg in enumerate(args):
            if arg == "--label":
                key, value = args[index + 1].split("=", 1)
                self.run_labels[key] = value
        if "-v" in args:
            source = Path(args[args.index("-v") + 1].split(":", 1)[0])
            self.mount_source = source
            rdb = source / "dump.rdb"
            self.mounted_rdb = rdb.read_bytes() if rdb.is_file() else None
        return self._done(args, stdout=f"{self.container_id}\n".encode())

    def _handle_inspect(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        labels = {
            key: value
            for key, value in self.run_labels.items()
            if key not in self.inspect_drop_labels
        }
        labels.update(self.inspect_extra_labels)
        payload = [
            {
                "Id": self.inspect_id or self.container_id,
                "Name": f"/{self.container}",
                "Config": {"Labels": labels},
            }
        ]
        return self._done(args, stdout=json.dumps(payload).encode())

    def _handle_exec(
        self,
        args: list[str],
        argv: list[str],
        *,
        piped: bool,
        stdin: IO[bytes] | None,
    ) -> subprocess.CompletedProcess[bytes]:
        program = argv[0]
        if program == "pg_isready":
            self._pg_ready_seen += 1
            ready = self._pg_ready_seen >= self.pg_ready_after
            return self._done(args, returncode=0 if ready else 2)
        if program == "psql":
            return self._handle_psql(args, argv, piped=piped, stdin=stdin)
        if program == "createdb":
            return self._done(args)
        if program == "pg_restore":
            assert piped, "pg_restore must stream the dump on stdin"
            assert stdin is not None
            self.stdin_payloads["dump"] = stdin.read()
            return self._done(
                args,
                returncode=self.pg_restore_returncode,
                stderr=self.pg_restore_stderr,
            )
        if program == "sh":
            return self._handle_redis(args, argv)
        raise AssertionError(f"unexpected exec program: {argv}")

    def _handle_psql(
        self,
        args: list[str],
        argv: list[str],
        *,
        piped: bool,
        stdin: IO[bytes] | None,
    ) -> subprocess.CompletedProcess[bytes]:
        if "-f" in argv:
            assert piped, "globals replay must stream on stdin"
            assert stdin is not None
            assert "ON_ERROR_STOP" not in " ".join(argv)
            self.stdin_payloads["globals"] = stdin.read()
            return self._done(args, returncode=self.globals_returncode, stderr=self.globals_stderr)
        sql = argv[argv.index("-c") + 1]
        if "pg_roles" in sql:
            rows = "\n".join(
                f"{name}\t{'t' if is_super else 'f'}\t{'t' if can_login else 'f'}"
                for name, is_super, can_login in self.role_rows
            )
            return self._done(args, stdout=f"{rows}\n".encode())
        if "pg_class" in sql:
            rows = "\n".join(
                f"{kind}\t{count}" for kind, count in sorted(self.schema_object_counts.items())
            )
            return self._done(args, stdout=f"{rows}\n".encode())
        if "count(*)" in sql.lower():
            table = sql.rsplit("FROM ", 1)[1].strip().strip('"')
            assert table in self.table_counts, f"unexpected count target: {sql}"
            return self._done(args, stdout=f"{self.table_counts[table]}\n".encode())
        raise AssertionError(f"unexpected sql: {sql}")

    def _handle_redis(self, args: list[str], argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        command = argv[2]
        assert '"$GOBBY_FALKORDB_PASSWORD"' in command, "password must resolve inside the container"
        if command.endswith("PING"):
            self._falkor_ready_seen += 1
            ready = self._falkor_ready_seen >= self.falkor_ready_after
            return self._done(args, stdout=b"PONG\n" if ready else b"")
        if "GRAPH.LIST" in command:
            return self._done(args, stdout=("\n".join(self.graphs) + "\n").encode())
        if "GRAPH.QUERY" in command:
            graph = next(name for name in self.graph_counts if f"GRAPH.QUERY {name} " in command)
            metric = "nodes" if "count(n)" in command else "edges"
            return self._done(args, stdout=f"{self.graph_counts[graph][metric]}\n".encode())
        raise AssertionError(f"unexpected redis command: {command}")

    @staticmethod
    def _done(
        args: list[str],
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr=stderr
        )

    # -- assertion helpers ------------------------------------------------
    def argv_starting(self, *prefix: str) -> list[list[str]]:
        return [call for call in self.calls if call[: len(prefix)] == list(prefix)]

    def exec_argv_containing(self, needle: str) -> list[list[str]]:
        return [call for call in self.calls if call[1] == "exec" and any(needle in a for a in call)]

    @property
    def run_argv(self) -> list[str]:
        runs = self.argv_starting("docker", "run")
        assert len(runs) == 1, f"expected exactly one docker run: {runs}"
        return runs[0]

    @property
    def container(self) -> str:
        argv = self.run_argv
        return argv[argv.index("--name") + 1]


def _install(monkeypatch: pytest.MonkeyPatch, fake: _DockerFake) -> None:
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(_verify, "_PG_READY_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(_verify, "_FALKOR_READY_TIMEOUT_SECONDS", 5)


def _pg_fixture(tmp_path: Path) -> tuple[Path, Path]:
    dump_path = tmp_path / "gobby.dump"
    dump_path.write_bytes(b"PGDMP-payload")
    globals_path = tmp_path / "globals.sql"
    globals_path.write_bytes(b"CREATE ROLE gobby;\n")
    return dump_path, globals_path


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def test_verify_postgres_restore_happy_path_drives_prod_image_without_ports_or_volumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.role_rows = [("postgres", True, True), ("gobby", False, True)]
    fake.table_counts = {"tasks": 3, "sessions": 5}
    fake.schema_object_counts = {"index": 8, "table": 2}
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    state, details = _verify.verify_postgres_restore(
        dump_path,
        globals_path,
        expected_probes={"tasks": 3, "sessions": 5},
        expected_schema_objects={"index": 8, "table": 2},
        expected_roles=[
            RoleExpectation("gobby", rolsuper=False, rolcanlogin=True),
            RoleExpectation("postgres", rolsuper=True, rolcanlogin=True),
        ],
    )

    assert state.verified is True
    assert state.method == "scratch-pg-restore+roles+rows+schema-counts"
    assert state.timestamp is not None
    assert state.timestamp.endswith("+00:00")
    assert details == {
        "tables_checked": 2,
        "roles_checked": 2,
        "schema_objects_checked": 2,
        "scratch_container": fake.container,
    }

    run_argv = fake.run_argv
    assert run_argv[:3] == ["docker", "run", "-d"]
    assert fake.container.startswith("gobby-hub-verify-pg-")
    assert "gobby-postgres-local:18-pgsearch" in run_argv
    assert "shared_preload_libraries=pg_search,pgaudit" in run_argv
    assert not {"-p", "--publish", "-v", "--volume"} & set(run_argv)
    assert any(arg.startswith("POSTGRES_PASSWORD=") and len(arg) > 20 for arg in run_argv)
    assert "POSTGRES_USER=postgres" in run_argv
    assert "POSTGRES_DB=postgres" in run_argv
    labels = [run_argv[index + 1] for index, arg in enumerate(run_argv) if arg == "--label"]
    assert "io.gobby.disposable=true" in labels
    assert any(label.startswith("io.gobby.run-id=") for label in labels)

    assert fake.stdin_payloads["globals"] == globals_path.read_bytes()
    assert fake.stdin_payloads["dump"] == dump_path.read_bytes()
    assert fake.exec_argv_containing("createdb")[0][-3:] == ["-U", "postgres", "gobby"]
    restore_argv = fake.exec_argv_containing("pg_restore")[0]
    assert restore_argv[2:4] == ["-i", fake.container]
    assert restore_argv[4:] == ["pg_restore", "-U", "postgres", "-d", "gobby"]
    assert "--no-owner" not in restore_argv
    assert fake.argv_starting("docker", "container", "inspect", fake.container_id)
    assert fake.argv_starting("docker", "rm", "-f", "-v", fake.container_id)


@pytest.mark.parametrize(
    ("inspect_id", "drop_labels", "extra_labels"),
    [
        ("deadbeef", set(), {}),
        (None, {"io.gobby.disposable"}, {}),
        (None, set(), {"io.gobby.run-id": "wrong-run"}),
        (None, set(), {"com.docker.compose.project": "outside-gobby"}),
    ],
)
def test_verify_postgres_restore_refuses_cleanup_without_exact_disposal_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    inspect_id: str | None,
    drop_labels: set[str],
    extra_labels: dict[str, str],
) -> None:
    fake = _DockerFake()
    fake.inspect_id = inspect_id
    fake.inspect_drop_labels = drop_labels
    fake.inspect_extra_labels = extra_labels
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    _verify.verify_postgres_restore(
        dump_path,
        globals_path,
        expected_probes={},
        expected_roles=[],
    )

    assert not fake.argv_starting("docker", "rm", "-f")
    assert "refusing to remove scratch container" in capsys.readouterr().err


def test_verify_postgres_restore_uses_repair_escape_for_restored_database_probes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.role_rows = [("gobby", False, True)]
    fake.table_counts = {"tasks": 1}
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    _verify.verify_postgres_restore(
        dump_path,
        globals_path,
        expected_probes={"tasks": 1},
        expected_roles=[RoleExpectation("gobby", rolsuper=False, rolcanlogin=True)],
    )

    row_count_argv = fake.exec_argv_containing("count(*)")[0]
    assert row_count_argv[2:5] == [
        "-e",
        "PGOPTIONS=-c event_triggers=off",
        fake.container,
    ]
    role_argv = fake.exec_argv_containing("pg_roles")[0]
    assert "PGOPTIONS=-c event_triggers=off" not in role_argv


def test_verify_postgres_restore_ignores_globals_replay_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.globals_returncode = 1
    fake.role_rows = [("gobby", False, True)]
    fake.table_counts = {"tasks": 1}
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    state, _details = _verify.verify_postgres_restore(
        dump_path,
        globals_path,
        expected_probes={"tasks": 1},
        expected_roles=[RoleExpectation("gobby", rolsuper=False, rolcanlogin=True)],
    )

    assert state.verified is True


def test_verify_postgres_restore_polls_until_scratch_cluster_is_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.pg_ready_after = 3
    fake.role_rows = [("gobby", False, True)]
    fake.table_counts = {"tasks": 1}
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    state, _details = _verify.verify_postgres_restore(
        dump_path,
        globals_path,
        expected_probes={"tasks": 1},
        expected_roles=[RoleExpectation("gobby", rolsuper=False, rolcanlogin=True)],
    )

    assert state.verified is True
    assert len(fake.exec_argv_containing("pg_isready")) == 3


def test_verify_postgres_restore_raises_when_scratch_cluster_never_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.pg_ready_after = 10_000
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_postgres_restore(
            dump_path,
            globals_path,
            expected_probes={},
            expected_roles=[],
        )

    assert "ready" in str(excinfo.value).lower()
    assert fake.argv_starting("docker", "rm", "-f", "-v", fake.container_id)


def test_verify_postgres_restore_raises_on_role_attribute_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.role_rows = [("gobby", False, True)]
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_postgres_restore(
            dump_path,
            globals_path,
            expected_probes={},
            expected_roles=[RoleExpectation("gobby", rolsuper=True, rolcanlogin=True)],
        )

    message = str(excinfo.value)
    assert "gobby" in message
    assert "rolsuper" in message
    assert fake.argv_starting("docker", "rm", "-f", "-v", fake.container_id)
    assert not fake.exec_argv_containing("pg_restore")


def test_verify_postgres_restore_raises_on_missing_role(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.role_rows = [("gobby", False, True)]
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_postgres_restore(
            dump_path,
            globals_path,
            expected_probes={},
            expected_roles=[RoleExpectation("gobby_ro", rolsuper=False, rolcanlogin=True)],
        )

    assert "gobby_ro" in str(excinfo.value)
    assert fake.argv_starting("docker", "rm", "-f", "-v", fake.container_id)


class _SourceRoleCursor:
    def __init__(self, rows: list[tuple[str, bool, bool]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str, bool, bool]]:
        return list(self._rows)


class _SourceRoleConnection:
    """A source cluster whose role list still carries managed principals."""

    def __init__(self, rows: list[tuple[str, bool, bool]]) -> None:
        self._rows = rows

    def __enter__(self) -> _SourceRoleConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, _sql: str, _params: object = None) -> _SourceRoleCursor:
        return _SourceRoleCursor(self._rows)


def test_verify_postgres_restore_ignores_managed_principals_drained_before_dump(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # #20985: interactive/maintenance principals present at snapshot time are
    # drained before pg_dumpall, so the scratch cluster never carries them.
    source_rows = [
        ("gobby", False, True),
        ("gobby_ix_e94cf5ac3163ddb1_1", False, True),
        ("gobby_ix_fa3149beb8d9b5c5_89", False, True),
        ("gobby_mnt_0123456789abcdef0123456789abcdef_4", False, True),
        ("gobby_agent_0123456789abcdef0123456789abcdef_9", False, True),
    ]
    monkeypatch.setattr(
        psycopg, "connect", lambda *_args, **_kwargs: _SourceRoleConnection(source_rows)
    )
    expected_roles = hub_cli._role_expectations(
        stores.collect_source_roles("postgresql://gobby:pw@localhost:60891/gobby")
    )
    assert [role.rolname for role in expected_roles] == ["gobby"]

    fake = _DockerFake()
    fake.role_rows = [("gobby", False, True)]
    fake.table_counts = {"tasks": 0}
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    state, details = _verify.verify_postgres_restore(
        dump_path,
        globals_path,
        expected_probes={"tasks": 0},
        expected_roles=expected_roles,
    )

    assert state.verified is True
    assert details["roles_checked"] == 1


def test_verify_postgres_restore_skips_attribute_compare_for_bootstrap_postgres_role(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.role_rows = [("postgres", True, True)]
    fake.table_counts = {"tasks": 0}
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    state, details = _verify.verify_postgres_restore(
        dump_path,
        globals_path,
        expected_probes={"tasks": 0},
        expected_roles=[RoleExpectation("postgres", rolsuper=False, rolcanlogin=False)],
    )

    assert state.verified is True
    assert details["roles_checked"] == 1


def test_verify_postgres_restore_raises_on_row_count_mismatch_naming_the_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.role_rows = [("gobby", False, True)]
    fake.table_counts = {"tasks": 3, "sessions": 4}
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_postgres_restore(
            dump_path,
            globals_path,
            expected_probes={"tasks": 3, "sessions": 5},
            expected_roles=[RoleExpectation("gobby", rolsuper=False, rolcanlogin=True)],
        )

    message = str(excinfo.value)
    assert "sessions" in message
    assert "5" in message
    assert "4" in message
    assert "tasks" not in message
    assert fake.argv_starting("docker", "rm", "-f", "-v", fake.container_id)


def test_verify_postgres_restore_rejects_schema_object_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.role_rows = [("gobby", False, True)]
    fake.table_counts = {"tasks": 3}
    fake.schema_object_counts = {"index": 7, "table": 1}
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_postgres_restore(
            dump_path,
            globals_path,
            expected_probes={"tasks": 3},
            expected_schema_objects={"index": 8, "table": 1},
            expected_roles=[RoleExpectation("gobby", rolsuper=False, rolcanlogin=True)],
        )

    assert "schema object" in str(excinfo.value).lower()
    assert "index" in str(excinfo.value)


def test_verify_postgres_restore_raises_with_stderr_tail_when_pg_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.role_rows = [("gobby", False, True)]
    fake.pg_restore_returncode = 1
    fake.pg_restore_stderr = b"pg_restore: error: could not execute query: relation missing"
    _install(monkeypatch, fake)
    dump_path, globals_path = _pg_fixture(tmp_path)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_postgres_restore(
            dump_path,
            globals_path,
            expected_probes={},
            expected_roles=[RoleExpectation("gobby", rolsuper=False, rolcanlogin=True)],
        )

    assert "relation missing" in str(excinfo.value)
    assert fake.argv_starting("docker", "rm", "-f", "-v", fake.container_id)


def test_verify_postgres_restore_raises_when_dump_missing(tmp_path: Path) -> None:
    globals_path = tmp_path / "globals.sql"
    globals_path.write_bytes(b"")

    with pytest.raises(click.ClickException):
        _verify.verify_postgres_restore(
            tmp_path / "absent.dump",
            globals_path,
            expected_probes={},
            expected_roles=[],
        )


def test_verify_postgres_restore_refuses_symlinked_artifact_parent(tmp_path: Path) -> None:
    real_dir = tmp_path / "real-backup"
    real_dir.mkdir()
    (real_dir / "gobby.dump").write_bytes(b"dump")
    (real_dir / "globals.sql").write_bytes(b"globals")
    linked_dir = tmp_path / "backup-link"
    linked_dir.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_postgres_restore(
            linked_dir / "gobby.dump",
            linked_dir / "globals.sql",
            expected_probes={},
            expected_roles=[],
        )

    assert "symlink" in str(excinfo.value).lower()
    assert str(linked_dir) in str(excinfo.value)


# ---------------------------------------------------------------------------
# FalkorDB
# ---------------------------------------------------------------------------


def _falkor_rdb(tmp_path: Path) -> Path:
    rdb_path = tmp_path / "dump.rdb"
    rdb_path.write_bytes(b"REDIS0011-graph-payload")
    return rdb_path


def test_verify_falkordb_restore_seeds_scratch_dir_and_verifies_graph_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.falkor_ready_after = 2
    fake.graphs = ["memory", "code"]
    fake.graph_counts = {
        "code": {"nodes": 2, "edges": 1},
        "memory": {"nodes": 5, "edges": 4},
    }
    _install(monkeypatch, fake)
    rdb_path = _falkor_rdb(tmp_path)

    state, details = _verify.verify_falkordb_restore(rdb_path, fake.graph_counts)

    assert state.verified is True
    assert state.method == "falkordb-scratch-rdb-load+graph-counts"
    assert state.timestamp is not None
    assert details == {
        "graphs_verified": 2,
        "graph_inventory_verified": fake.graph_counts,
    }

    run_argv = fake.run_argv
    assert fake.container.startswith("gobby-hub-verify-falkor-")
    assert "falkordb/falkordb:latest" in run_argv
    assert not {"-p", "--publish"} & set(run_argv)
    mount = run_argv[run_argv.index("-v") + 1]
    assert mount.endswith(":/var/lib/falkordb/data")
    assert fake.mounted_rdb == rdb_path.read_bytes()
    assert rdb_path.is_file(), "source RDB must be copied, not moved"

    password_args = [arg for arg in run_argv if arg.startswith("REDIS_ARGS=")]
    assert password_args
    assert password_args[0].startswith("REDIS_ARGS=--requirepass ")
    password = password_args[0].split("--requirepass ", 1)[1]
    assert f"GOBBY_FALKORDB_PASSWORD={password}" in run_argv
    assert not any(password in arg for call in fake.exec_argv_containing("sh") for arg in call)

    assert len(fake.exec_argv_containing("PING")) == 2
    assert fake.exec_argv_containing("GRAPH.LIST")
    assert fake.argv_starting("docker", "rm", "-f", "-v", fake.container_id)
    assert fake.mount_source is not None
    assert not fake.mount_source.exists()


def test_verify_falkordb_restore_raises_on_graph_set_mismatch_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.graphs = ["memory"]
    _install(monkeypatch, fake)
    rdb_path = _falkor_rdb(tmp_path)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_falkordb_restore(
            rdb_path,
            {"memory": {"nodes": 1, "edges": 0}, "code": {"nodes": 1, "edges": 0}},
        )

    assert "code" in str(excinfo.value)
    assert fake.argv_starting("docker", "rm", "-f", "-v", fake.container_id)
    assert fake.mount_source is not None
    assert not fake.mount_source.exists()


def test_verify_falkordb_restore_raises_when_container_never_answers_ping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.falkor_ready_after = 10_000
    _install(monkeypatch, fake)
    rdb_path = _falkor_rdb(tmp_path)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_falkordb_restore(rdb_path, {"memory": {"nodes": 1, "edges": 0}})

    assert "ready" in str(excinfo.value).lower()
    assert fake.argv_starting("docker", "rm", "-f", "-v", fake.container_id)


def test_verify_falkordb_restore_raises_when_rdb_missing(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException):
        _verify.verify_falkordb_restore(
            tmp_path / "absent.rdb", {"memory": {"nodes": 1, "edges": 0}}
        )


def test_verify_falkordb_restore_rejects_same_graph_names_with_missing_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _DockerFake()
    fake.graphs = ["memory"]
    fake.graph_counts = {"memory": {"nodes": 2, "edges": 1}}
    _install(monkeypatch, fake)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_falkordb_restore(
            _falkor_rdb(tmp_path),
            {"memory": {"nodes": 3, "edges": 1}},
        )

    assert "memory" in str(excinfo.value)
    assert "nodes" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------


class _FakeCountResult:
    def __init__(self, count: int) -> None:
        self.count = count


class _FakeSnapshotsApi:
    def __init__(self, owner: _FakeQdrantClient) -> None:
        self._owner = owner

    def recover_from_uploaded_snapshot(
        self,
        *,
        collection_name: str,
        snapshot: IO[bytes],
        wait: bool = False,
    ) -> object:
        self._owner.recovered.append((collection_name, wait, snapshot.read()))
        return object()


class _FakeHttp:
    def __init__(self, owner: _FakeQdrantClient) -> None:
        self.snapshots_api = _FakeSnapshotsApi(owner)


class _FakeQdrantClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.recovered: list[tuple[str, bool, bytes]] = []
        self.counted: list[tuple[str, bool]] = []
        self.deleted: list[str] = []
        self.closed = False
        self.counts: dict[str, int] = {}
        self.points: dict[str, list[dict[str, object]]] = {}
        self.http = _FakeHttp(self)

    def count(self, collection_name: str, exact: bool = True) -> _FakeCountResult:
        self.counted.append((collection_name, exact))
        return _FakeCountResult(self.counts.get(collection_name, 0))

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
        return self.points.get(collection_name, []), None

    def delete_collection(self, collection_name: str) -> bool:
        self.deleted.append(collection_name)
        return True

    def close(self) -> None:
        self.closed = True


def _install_qdrant(
    monkeypatch: pytest.MonkeyPatch,
    counts: dict[str, int],
    points: dict[str, list[dict[str, object]]] | None = None,
) -> _FakeQdrantClient:
    client = _FakeQdrantClient()
    client.counts = counts
    client.points = points or {
        name: [{"id": count, "payload": {"value": count}}] for name, count in counts.items()
    }

    def _factory(**kwargs: Any) -> _FakeQdrantClient:
        client.kwargs = kwargs
        return client

    monkeypatch.setattr(_verify, "QdrantClient", _factory)
    return client


def _snapshot(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.snapshot"
    path.write_bytes(f"snapshot-of-{name}".encode())
    return path


def test_verify_qdrant_restore_recovers_each_collection_into_scratch_and_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    points = {
        "hub_backup_verify_memory": [{"id": 1, "payload": {"kind": "memory"}}],
        "hub_backup_verify_code": [{"id": 2, "payload": {"kind": "code"}}],
    }
    client = _install_qdrant(
        monkeypatch,
        {"hub_backup_verify_memory": 7, "hub_backup_verify_code": 2},
        points,
    )
    snapshots = {"memory": _snapshot(tmp_path, "memory"), "code": _snapshot(tmp_path, "code")}

    state, details = _verify.verify_qdrant_restore(
        "http://localhost:6333",
        "sekrit",
        snapshots,
        {"memory": 7, "code": 2},
        {
            "memory": _point_digest(points["hub_backup_verify_memory"]),
            "code": _point_digest(points["hub_backup_verify_code"]),
        },
    )

    assert state.verified is True
    assert state.method == "qdrant-scratch-recover+point-content-digest"
    assert state.timestamp is not None
    assert details == {
        "collections_checked": 2,
        "points_verified": {"memory": 7, "code": 2},
        "content_digests_verified": {
            "memory": _point_digest(points["hub_backup_verify_memory"]),
            "code": _point_digest(points["hub_backup_verify_code"]),
        },
    }

    assert client.kwargs["url"] == "http://localhost:6333"
    assert client.kwargs["api_key"] == "sekrit"
    assert client.kwargs["timeout"] == 120
    assert sorted(client.recovered) == sorted(
        [
            ("hub_backup_verify_memory", True, b"snapshot-of-memory"),
            ("hub_backup_verify_code", True, b"snapshot-of-code"),
        ]
    )
    assert sorted(client.counted) == sorted(
        [("hub_backup_verify_code", True), ("hub_backup_verify_memory", True)]
    )
    assert sorted(client.deleted) == ["hub_backup_verify_code", "hub_backup_verify_memory"]


def test_verify_qdrant_restore_refuses_local_mode_without_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _factory(**_kwargs: Any) -> _FakeQdrantClient:
        raise AssertionError("client must not be constructed without a URL")

    monkeypatch.setattr(_verify, "QdrantClient", _factory)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_qdrant_restore(None, None, {"memory": _snapshot(tmp_path, "memory")}, {}, {})

    assert "url" in str(excinfo.value).lower()


def test_verify_qdrant_restore_raises_on_count_mismatch_and_still_deletes_scratch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _install_qdrant(monkeypatch, {"hub_backup_verify_memory": 3})

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_qdrant_restore(
            "http://localhost:6333",
            None,
            {"memory": _snapshot(tmp_path, "memory")},
            {"memory": 9},
            {"memory": "0" * 64},
        )

    message = str(excinfo.value)
    assert "memory" in message
    assert "9" in message
    assert "3" in message
    assert client.deleted == ["hub_backup_verify_memory"]


def test_verify_qdrant_restore_raises_when_expected_count_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_qdrant(monkeypatch, {"hub_backup_verify_memory": 3})

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_qdrant_restore(
            "http://localhost:6333",
            None,
            {"memory": _snapshot(tmp_path, "memory")},
            {},
            {},
        )

    assert "memory" in str(excinfo.value)


def test_verify_qdrant_restore_rejects_same_count_with_different_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    restored = [{"id": 1, "payload": {"text": "truncated"}}]
    source = [{"id": 1, "payload": {"text": "complete"}}]
    client = _install_qdrant(
        monkeypatch,
        {"hub_backup_verify_memory": 1},
        {"hub_backup_verify_memory": restored},
    )

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_qdrant_restore(
            "http://localhost:6333",
            None,
            {"memory": _snapshot(tmp_path, "memory")},
            {"memory": 1},
            {"memory": _point_digest(source)},
        )

    assert "content digest" in str(excinfo.value).lower()
    assert client.deleted == ["hub_backup_verify_memory"]


# ---------------------------------------------------------------------------
# Volume archives
# ---------------------------------------------------------------------------


def _make_archive(tmp_path: Path, name: str, members: int) -> Path:
    archive = tmp_path / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for index in range(members):
            payload = tmp_path / f"{name}-{index}.bin"
            payload.write_bytes(b"x" * (32 * (index + 1)))
            tar.add(payload, arcname=f"{name}/{index}.bin")
            payload.unlink()
    return archive


def _record_scratch_dirs(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Path], list[list[str]]]:
    created: list[Path] = []
    extracted: list[list[str]] = []
    real_mkdtemp = tempfile.mkdtemp
    real_rmtree = shutil.rmtree

    def _mkdtemp(*, prefix: str | None = None) -> str:
        path = Path(real_mkdtemp(prefix=prefix)).resolve(strict=True)
        created.append(path)
        return str(path)

    def _rmtree(path: Any, **kwargs: Any) -> None:
        target = Path(path)
        if target in created:
            extracted.append(sorted(str(p.relative_to(target)) for p in target.rglob("*")))
        real_rmtree(path, **kwargs)

    monkeypatch.setattr(tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(shutil, "rmtree", _rmtree)
    return created, extracted


def test_verify_volume_archives_extracts_each_archive_and_counts_members(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created, extracted = _record_scratch_dirs(monkeypatch)
    archives = {
        "gobby_qdrant_data": _make_archive(tmp_path, "qdrant", 2),
        "gobby_falkordb_data": _make_archive(tmp_path, "falkor", 1),
    }
    inventories = {
        volume: archive_inventory(path, label=volume).to_dict() for volume, path in archives.items()
    }

    state, details = _verify.verify_volume_archives(archives, inventories)

    assert state.verified is True
    assert state.method == "tar-extract+source-content-inventory"
    assert state.timestamp is not None
    assert details == {
        "archives": {"gobby_qdrant_data": 2, "gobby_falkordb_data": 1},
        "source_inventories_verified": inventories,
    }
    assert extracted == [
        ["falkor", "falkor/0.bin"],
        ["qdrant", "qdrant/0.bin", "qdrant/1.bin"],
    ]
    assert created
    assert not any(path.exists() for path in created)


def test_verify_volume_archives_raises_on_empty_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created, _extracted = _record_scratch_dirs(monkeypatch)
    archives = {"gobby_empty": _make_archive(tmp_path, "empty", 0)}

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_volume_archives(
            archives, {"gobby_empty": {"members": 1, "sha256": "0" * 64}}
        )

    assert "gobby_empty" in str(excinfo.value)
    assert not any(path.exists() for path in created)


def test_verify_volume_archives_raises_on_truncated_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created, _extracted = _record_scratch_dirs(monkeypatch)
    archive = _make_archive(tmp_path, "truncated", 3)
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_volume_archives(
            {"gobby_truncated": archive},
            {"gobby_truncated": {"members": 1, "sha256": "0" * 64}},
        )

    assert "gobby_truncated" in str(excinfo.value)
    assert not any(path.exists() for path in created)


def test_verify_volume_archives_raises_on_missing_archive(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_volume_archives(
            {"gobby_absent": tmp_path / "nope.tar.gz"},
            {"gobby_absent": {"members": 1, "sha256": "0" * 64}},
        )

    assert "gobby_absent" in str(excinfo.value)


def test_verify_volume_archives_rejects_same_members_with_different_bytes(
    tmp_path: Path,
) -> None:
    source = _make_archive(tmp_path, "source", 1)
    expected = archive_inventory(source, label="source").to_dict()
    backup = tmp_path / "backup.tar.gz"
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"different-content-with-the-same-member-count")
    with tarfile.open(backup, "w:gz") as tar:
        tar.add(payload, arcname="source/0.bin")

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_volume_archives({"volume": backup}, {"volume": expected})

    assert "content inventory" in str(excinfo.value).lower()


def test_verify_volume_archives_refuses_symlink_member(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("data/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        tar.addfile(member)

    with pytest.raises(click.ClickException) as excinfo:
        _verify.verify_volume_archives(
            {"volume": archive},
            {"volume": {"members": 1, "sha256": "0" * 64}},
        )

    assert "symlink" in str(excinfo.value).lower()
    assert "data/link" in str(excinfo.value)
