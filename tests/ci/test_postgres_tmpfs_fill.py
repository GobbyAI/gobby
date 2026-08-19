"""Opt-in destructive probe for PostgreSQL tmpfs exhaustion diagnostics."""

from __future__ import annotations

import os
import shlex
import uuid
from typing import Any

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.integration

_RUN_FILL_TEST_ENV = "GOBBY_RUN_POSTGRES_TMPFS_FILL_TEST"
_MAX_SAFE_TMPFS_KIB = 128 * 1024
_FILL_TMPFS_SIZE = "32M"


def _program_lines(conn: psycopg.Connection[Any], command: str) -> list[str]:
    conn.execute("TRUNCATE tmpfs_fill_program_output")
    conn.execute(
        sql.SQL("COPY tmpfs_fill_program_output FROM PROGRAM {}").format(sql.Literal(command))
    )
    return [row[0] for row in conn.execute("SELECT line FROM tmpfs_fill_program_output")]


def _resolve_pg_tool(conn: psycopg.Connection[Any], name: str) -> str:
    quoted = shlex.quote(name)
    lines = _program_lines(
        conn,
        f"command -v {quoted} || "
        f'{{ p=$(command -v postgres) && tool="${{p%/*}}/{name}" '
        f'&& test -x "$tool" && printf "%s\\n" "$tool"; }}',
    )
    assert lines and lines[0].strip(), f"{name} not found on PATH"
    return lines[0].strip()


@pytest.mark.skipif(
    os.environ.get(_RUN_FILL_TEST_ENV) != "1",
    reason=f"set {_RUN_FILL_TEST_ENV}=1 to run the destructive tmpfs fill probe",
)
def test_postgres_tmpfs_fill_reports_enospc(postgres_database_url: str) -> None:
    """A nested cluster reports ENOSPC without filling the test server's PGDATA."""
    suffix = uuid.uuid4().hex[:12]
    shm_dir = f"/dev/shm/gobby-pg-fill-{suffix}"
    isolated_dir = f"/tmp/gobby-pg-fill-{suffix}"
    port = 50_000 + int(suffix[:4], 16) % 10_000
    fill_mount: str | None = None

    with psycopg.connect(postgres_database_url, autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE tmpfs_fill_program_output (line text)")
        tmpfs_fields = _program_lines(conn, "df -Pk /dev/shm | tail -n 1")[0].split()
        tmpfs_size_kib = int(tmpfs_fields[1])
        if tmpfs_size_kib > _MAX_SAFE_TMPFS_KIB:
            cluster_dir = isolated_dir
            quoted_dir = shlex.quote(cluster_dir)
            _program_lines(
                conn,
                f"mkdir -m 700 -p -- {quoted_dir} && "
                f"mount -t tmpfs -o size={_FILL_TMPFS_SIZE},mode=700 tmpfs {quoted_dir}",
            )
            fill_mount = cluster_dir
        else:
            cluster_dir = shm_dir
            quoted_dir = shlex.quote(cluster_dir)
            _program_lines(conn, f"mkdir -m 700 -- {quoted_dir}")

        initdb_bin = shlex.quote(_resolve_pg_tool(conn, "initdb"))
        pg_ctl_bin = shlex.quote(_resolve_pg_tool(conn, "pg_ctl"))
        try:
            _program_lines(
                conn,
                f"{initdb_bin} -D {quoted_dir} -U postgres --no-sync >/dev/null 2>&1",
            )
            server_options = shlex.quote(
                f"-p {port} -k {cluster_dir} -c listen_addresses='' "
                "-c fsync=off -c synchronous_commit=off"
            )
            _program_lines(
                conn,
                f"{pg_ctl_bin} -D {quoted_dir} -l {quoted_dir}/server.log "
                f"-o {server_options} -w start >/dev/null 2>&1",
            )

            fill_sql = (
                "CREATE TABLE fill AS SELECT g, "
                "(SELECT string_agg(md5((g * 1000 + s)::text), '') "
                "FROM generate_series(1, 250) AS s) AS payload "
                "FROM generate_series(1, 10000) AS g"
            )
            _program_lines(
                conn,
                f"psql -X -h {quoted_dir} -p {port} -U postgres -d postgres "
                f"-v ON_ERROR_STOP=1 -c {shlex.quote(fill_sql)} >/dev/null 2>&1 || true",
            )
            server_log = "\n".join(
                _program_lines(conn, f"tail -n 100 {quoted_dir}/server.log || true")
            )

            assert "No space left on device" in server_log
            assert "could not write" in server_log
        finally:
            cleanup = f"{pg_ctl_bin} -D {quoted_dir} -m immediate -w stop >/dev/null 2>&1 || true"
            if fill_mount is not None:
                cleanup += f"; umount {quoted_dir} >/dev/null 2>&1 || true"
            cleanup += f"; rm -r -- {quoted_dir}"
            _program_lines(conn, cleanup)
