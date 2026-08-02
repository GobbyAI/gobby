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


def _program_lines(conn: psycopg.Connection[Any], command: str) -> list[str]:
    conn.execute("TRUNCATE tmpfs_fill_program_output")
    conn.execute(
        sql.SQL("COPY tmpfs_fill_program_output FROM PROGRAM {}").format(sql.Literal(command))
    )
    return [row[0] for row in conn.execute("SELECT line FROM tmpfs_fill_program_output")]


@pytest.mark.skipif(
    os.environ.get(_RUN_FILL_TEST_ENV) != "1",
    reason=f"set {_RUN_FILL_TEST_ENV}=1 to run the destructive tmpfs fill probe",
)
def test_postgres_tmpfs_fill_reports_enospc(postgres_database_url: str) -> None:
    """A nested cluster reports ENOSPC without filling the test server's PGDATA."""
    suffix = uuid.uuid4().hex[:12]
    cluster_dir = f"/dev/shm/gobby-pg-fill-{suffix}"
    quoted_dir = shlex.quote(cluster_dir)
    port = 50_000 + int(suffix[:4], 16) % 10_000
    postgres_bin = "/usr/lib/postgresql/18/bin"

    with psycopg.connect(postgres_database_url, autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE tmpfs_fill_program_output (line text)")
        tmpfs_fields = _program_lines(conn, "df -Pk /dev/shm | tail -n 1")[0].split()
        tmpfs_size_kib = int(tmpfs_fields[1])
        if tmpfs_size_kib > _MAX_SAFE_TMPFS_KIB:
            pytest.skip(
                f"refusing to fill /dev/shm larger than {_MAX_SAFE_TMPFS_KIB} KiB "
                f"(found {tmpfs_size_kib} KiB)"
            )

        _program_lines(conn, f"mkdir -m 700 -- {quoted_dir}")
        try:
            _program_lines(
                conn,
                f"{postgres_bin}/initdb -D {quoted_dir} -U postgres --no-sync >/dev/null 2>&1",
            )
            server_options = shlex.quote(
                f"-p {port} -k {cluster_dir} -c listen_addresses='' "
                "-c fsync=off -c synchronous_commit=off"
            )
            _program_lines(
                conn,
                f"{postgres_bin}/pg_ctl -D {quoted_dir} -l {quoted_dir}/server.log "
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
            _program_lines(
                conn,
                f"{postgres_bin}/pg_ctl -D {quoted_dir} -m immediate -w stop "
                f">/dev/null 2>&1 || true; rm -r -- {quoted_dir}",
            )
