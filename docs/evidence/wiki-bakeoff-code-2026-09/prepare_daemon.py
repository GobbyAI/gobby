"""Prepare the isolated daemon through Gobby's normal schema/config interfaces."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import quote, urlparse

import yaml
from provision_environment import DEFAULT_RUNTIME_ROOT, PORTS, _write_json, _write_text
from runtime_boundary import assert_owned_runtime, contained_file
from validate_environment import _load_env_file


def isolated_environment(root: Path, ambient: dict[str, str]) -> dict[str, str]:
    result = {
        name: value
        for name, value in ambient.items()
        if not name.startswith(("GOBBY_", "OTEL_"))
        and not name.endswith("_API_KEY")
        and name not in {"DATABASE_URL", "PGPASSWORD", "REDISCLI_AUTH", "PYTHONPATH"}
    }
    result.update(
        {
            "GOBBY_HOME": str(root / "gobby-home"),
            "GOBBY_NATIVE_BIN_DIR": str(root / "gobby-home/bin"),
            "GOBBY_TEST_PROTECT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            # Model discovery launches installed CLIs and seeds their global trust stores.
            # This daemon serves gcode datastores only; comparator CLIs run separately.
            "PATH": os.pathsep.join((str(root / "tools/gobby/venv/bin"), "/usr/bin", "/bin")),
        }
    )
    return result


def preserve_image_probe(dsn: str) -> None:
    """Keep the image's audit fixture outside Gobby's native public schema."""
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as conn:
        row = conn.execute("SELECT to_regclass('public._pgaudit_probe')::oid").fetchone()
        assert row is not None
        if row[0] is None:
            return
        target = conn.execute(
            "SELECT to_regclass('bakeoff_image_audit._pgaudit_probe')::oid"
        ).fetchone()
        assert target == (None,), "ambiguous image audit probe; refusing to replace either table"
        count = conn.execute("SELECT count(*) FROM public._pgaudit_probe").fetchone()
        conn.execute("CREATE SCHEMA IF NOT EXISTS bakeoff_image_audit")
        conn.execute("ALTER TABLE public._pgaudit_probe SET SCHEMA bakeoff_image_audit")
        assert (
            conn.execute("SELECT to_regclass('bakeoff_image_audit._pgaudit_probe')::oid").fetchone()
            == row
        )
        assert (
            conn.execute("SELECT count(*) FROM bakeoff_image_audit._pgaudit_probe").fetchone()
            == count
        )


def record_preparation(root: Path, revision: int) -> None:
    _write_json(
        root / f"receipts/daemon-preparation-rev-{revision}.json",
        {
            "source_root": str(root / "sources/gobby"),
            "gobby_home": str(root / "gobby-home"),
            "native_bin_dir": str(root / "gobby-home/bin"),
            "config_revision": revision,
            "schema_applied": True,
            "models_called": 0,
            "automatic_indexing": False,
            "cron_enabled": False,
        },
    )


def prepare(root: Path) -> None:
    # These changes affect this isolated setup process only, never its parent session.
    clean = isolated_environment(root, dict(os.environ))
    for name in tuple(os.environ):
        if name not in clean:
            os.environ.pop(name)
    os.environ.update(clean)

    import gobby
    from gobby.storage.config_mutations import ConfigMutations, ConfigPatch, SecretUpdate
    from gobby.storage.hub.postgres import PostgresHubDatabase
    from gobby.storage.schema_contract import apply_schema
    from gobby.utils.native_bin import resolve_native_bin

    assert Path(gobby.__file__).resolve().is_relative_to(root / "sources/gobby")
    assert resolve_native_bin("gdaemon") == str(root / "gobby-home/bin/gdaemon")
    credentials = _load_env_file(contained_file(root, "config/services.env"))
    dsn = (
        f"postgresql://{credentials['BAKEOFF_POSTGRES_USER']}:"
        f"{quote(credentials['BAKEOFF_POSTGRES_PASSWORD'], safe='')}@127.0.0.1:"
        f"{PORTS['postgres']}/{credentials['BAKEOFF_POSTGRES_DB']}"
    )
    parsed = urlparse(dsn)
    assert parsed.hostname == "127.0.0.1" and parsed.port == 61234
    assert parsed.path == "/gobby_bakeoff_21942"
    bootstrap = {
        "database_url": dsn,
        "daemon_port": PORTS["daemon_http"],
        "websocket_port": PORTS["daemon_ws"],
        "bind_host": "127.0.0.1",
        "ui_port": 59482,
        "datastore_mode": "local",
        "files_home": str(root / "gobby-home/files"),
    }
    _write_text(root / "gobby-home/bootstrap.yaml", yaml.safe_dump(bootstrap), mode=0o600)
    print("private bootstrap ready; applying pinned schema to owned PostgreSQL", flush=True)
    preserve_image_probe(dsn)
    apply_schema(dsn, schema="public")
    db = PostgresHubDatabase(dsn)
    try:
        mutations = ConfigMutations(db)
        revision = mutations.patch_internal(
            expected_revision=mutations.repository.current_revision(),
            source="wiki-bakeoff-21942",
            patch=ConfigPatch(
                values={
                    "test_mode": True,
                    "tmux.enabled": False,
                    "terminal_host.enabled": False,
                    "cron.enabled": False,
                    "tmux.socket_path": str(root / "gobby-home/tmux.sock"),
                    "memory.dream.enabled": False,
                    "gobby-tasks.expansion.enabled": False,
                    "gobby-tasks.validation.enabled": False,
                    "code_index.enabled": False,
                    "databases.qdrant.url": f"http://127.0.0.1:{PORTS['qdrant_http']}",
                    "databases.qdrant.port": PORTS["qdrant_http"],
                    "databases.falkordb.host": "127.0.0.1",
                    "databases.falkordb.port": PORTS["falkordb"],
                    "ai.embeddings.model": "text-embedding-nomic-embed-text-v1.5@f16",
                    "ai.embeddings.api_base": "http://127.0.0.1:1234/v1",
                    "ai.embeddings.dim": 768,
                    "ai.embeddings.query_prefix": "search_query: ",
                },
                secrets={
                    "databases.falkordb.password": SecretUpdate(
                        plaintext=credentials["BAKEOFF_FALKORDB_PASSWORD"]
                    ),
                },
            ),
        )
        record_preparation(root, revision.revision)
    finally:
        db.close()
    print("isolated schema and config prepared; no daemon or model generation started")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-session", required=True)
    args = parser.parse_args()
    assert_owned_runtime(DEFAULT_RUNTIME_ROOT, args.owner_session)
    prepare(DEFAULT_RUNTIME_ROOT)


if __name__ == "__main__":
    main()
