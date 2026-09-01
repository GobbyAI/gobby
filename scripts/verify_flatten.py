#!/usr/bin/env python3
"""Prove a schema flatten is lossless by applying two gdaemon binaries.

Each binary applies its embedded assets to its own scratch database on an
isolated hub and verifies the result. Both databases are dumped with
``pg_dump`` including owners and privileges; the dumps must be identical
after removing dump noise and database names. The dumps, receipts, and a
``gobby_*`` role snapshot land in ``--output-dir`` as the evidence record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from gobby.storage.schema_contract import DATABASE_URL_ENV, EXPECTED_IDENTITY_ENV

if __package__:
    from scripts.schema_diff import _postgres_client_connection, _run_postgres_client
else:
    from schema_diff import _postgres_client_connection, _run_postgres_client

DEFAULT_DATABASE_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
DEFAULT_OUTPUT_DIR = Path("docs/evidence/flatten-baseline-419")
_DUMP_SCHEMAS = ("public", "gobby_agent_auth")
_GDAEMON_TIMEOUT_SECONDS = 600
_ROLES_QUERY = """
SELECT r.rolname, r.rolcanlogin, r.rolinherit, r.rolbypassrls, r.rolcreaterole,
       array_remove(array_agg(m.rolname ORDER BY m.rolname), NULL) AS member_of
FROM pg_roles r
LEFT JOIN pg_auth_members am ON am.member = r.oid
LEFT JOIN pg_roles m ON m.oid = am.roleid
WHERE r.rolname LIKE 'gobby%%'
GROUP BY r.oid, r.rolname, r.rolcanlogin, r.rolinherit, r.rolbypassrls, r.rolcreaterole
ORDER BY r.rolname
"""


@dataclass(frozen=True)
class Snapshot:
    label: str
    identity: dict[str, Any]
    dump: str
    receipts: list[dict[str, Any]]
    roles: list[dict[str, Any]]


def _run_gdaemon(argv: list[str], *, env: dict[str, str], action: str) -> str:
    try:
        result = subprocess.run(  # nosec B603 - fixed binary path and argv, never shell=True
            argv,
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=_GDAEMON_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{action} timed out after {_GDAEMON_TIMEOUT_SECONDS}s") from None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"{action} failed: {detail}")
    return result.stdout


def gdaemon_identity(binary: Path) -> dict[str, Any]:
    output = _run_gdaemon(
        [str(binary), "schema", "version", "--json"],
        env=dict(os.environ),
        action=f"{binary} schema version",
    )
    identity = json.loads(output)
    if not isinstance(identity, dict):
        raise RuntimeError(f"{binary} schema version returned non-object JSON")
    return identity


@contextmanager
def scratch_database(base_url: str, label: str) -> Iterator[str]:
    name = f"gobby_flatten_{label}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database_url = make_conninfo(base_url, dbname=name)
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
        yield database_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def dump_schema(database_url: str, *, pg_dump: str) -> str:
    conninfo, env = _postgres_client_connection(database_url)
    return _run_postgres_client(
        [
            pg_dump,
            "--schema-only",
            *(f"--schema={schema}" for schema in _DUMP_SCHEMAS),
            "--dbname",
            conninfo,
        ],
        action="pg_dump",
        env=env,
    )


def normalize_dump(dump: str, database_name: str) -> str:
    """Strip comments, blank lines, pg_dump restrict tokens, and the database name."""
    kept = [
        line.replace(database_name, "<database>")
        for line in dump.splitlines()
        if line and not line.startswith(("--", "\\restrict", "\\unrestrict"))
    ]
    return "\n".join(kept) + "\n"


def _rows(database_url: str, query: str) -> list[dict[str, Any]]:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        return [dict(row) for row in connection.execute(query).fetchall()]


def snapshot(binary: Path, label: str, base_url: str, *, pg_dump: str) -> Snapshot:
    identity = gdaemon_identity(binary)
    with scratch_database(base_url, label) as database_url:
        env = dict(os.environ)
        env[DATABASE_URL_ENV] = database_url
        env[EXPECTED_IDENTITY_ENV] = json.dumps(identity)
        for action in ("apply", "verify"):
            _run_gdaemon(
                [str(binary), "schema", action],
                env=env,
                action=f"{label} gdaemon schema {action}",
            )
        database_name = str(conninfo_to_dict(database_url)["dbname"])
        return Snapshot(
            label=label,
            identity=identity,
            dump=normalize_dump(dump_schema(database_url, pg_dump=pg_dump), database_name),
            receipts=_rows(
                database_url,
                "SELECT version, filename, checksum FROM schema_migrations ORDER BY version",
            ),
            roles=_rows(database_url, _ROLES_QUERY),
        )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_evidence(output_dir: Path, old: Snapshot, new: Snapshot) -> tuple[str, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    diff = tuple(
        unified_diff(
            old.dump.splitlines(),
            new.dump.splitlines(),
            fromfile=f"{old.label}.dump.sql",
            tofile=f"{new.label}.dump.sql",
            lineterm="",
        )
    )
    for snap in (old, new):
        (output_dir / f"{snap.label}.dump.sql").write_text(snap.dump, encoding="utf-8")
        (output_dir / f"{snap.label}.receipts.json").write_text(
            json.dumps(snap.receipts, indent=2) + "\n", encoding="utf-8"
        )
    summary = {
        "dumps_identical": not diff,
        "roles_identical": old.roles == new.roles,
        "roles": old.roles,
        "old": {"identity": old.identity, "dump_sha256": _sha256(old.dump)},
        "new": {"identity": new.identity, "dump_sha256": _sha256(new.dump)},
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if diff:
        (output_dir / "dump.diff").write_text("\n".join(diff) + "\n", encoding="utf-8")
    return diff


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--old", type=Path, required=True, help="gdaemon with the old assets")
    parser.add_argument("--new", type=Path, required=True, help="gdaemon with the flattened assets")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="isolated hub allowed to CREATE DATABASE (never the live daemon hub)",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--pg-dump", default="pg_dump")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    old = snapshot(args.old, "old", args.database_url, pg_dump=args.pg_dump)
    new = snapshot(args.new, "new", args.database_url, pg_dump=args.pg_dump)
    diff = write_evidence(args.output_dir, old, new)
    print(f"old receipts: {len(old.receipts)} rows, new receipts: {len(new.receipts)} rows")
    print(f"roles identical: {old.roles == new.roles}")
    if diff:
        print(f"dumps differ ({len(diff)} diff lines); see {args.output_dir / 'dump.diff'}")
        return 1
    print(f"dumps identical: sha256={_sha256(new.dump)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
