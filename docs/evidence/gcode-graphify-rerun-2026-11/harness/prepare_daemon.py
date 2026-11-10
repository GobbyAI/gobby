"""Prepare the isolated daemon through Gobby's normal schema/config interfaces."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import yaml
from provision_environment import DEFAULT_RUNTIME_ROOT, PORTS, _write_json, _write_text
from runtime_boundary import EvidenceError, assert_owned_runtime, contained_file, require
from validate_environment import _load_env_file

# The embedding provider the isolated daemon is configured to use. The orchestrator ruling
# (gobby#19309, 86a74419) fixes these values. They are written into the isolated config;
# operator config is never read.
EMBEDDING_MODEL = "qwen3-embedding:0.6b-q8_0"
EMBEDDING_API_BASE = "http://localhost:11434/v1"
EMBEDDING_DIM = 1024
EMBEDDING_VALUES: dict[str, str | int] = {
    "ai.embeddings.model": EMBEDDING_MODEL,
    "ai.embeddings.api_base": EMBEDDING_API_BASE,
    "ai.embeddings.dim": EMBEDDING_DIM,
}


def embedding_readiness(api_base: str, model: str) -> None:
    """Refuse unless the configured embedding provider answers and lists the model.

    The probe sends no key, uses no proxy, and never logs the response body.
    """
    url = f"{api_base.rstrip('/')}/models"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=10) as response:
            body = response.read()
    except (OSError, http.client.HTTPException) as error:
        raise EvidenceError(f"embedding provider unreachable at {url}: {error}") from error
    try:
        listed = json.loads(body)
    except ValueError as error:
        raise EvidenceError(f"embedding provider at {url} did not answer with JSON") from error
    entries = listed.get("data") if isinstance(listed, dict) else None
    # A list, not a set: a listed id may be any JSON value, and only equality is needed here.
    listed_ids = [entry.get("id") for entry in entries or [] if isinstance(entry, dict)]
    if model not in listed_ids:
        raise EvidenceError(f"embedding provider at {url} does not list model {model!r}")


# The only ambient names a harness child inherits (plus every LC_* locale name). Everything
# else is dropped: credentials, agent sockets, proxies, and interpreter or tool controls.
AMBIENT_ALLOWLIST = frozenset({"LANG", "TERM", "TMPDIR"})


def isolated_environment(root: Path, ambient: dict[str, str]) -> dict[str, str]:
    """Build a child environment from an allowlist, never from the operator's ambient set."""
    result = {
        name: value
        for name, value in ambient.items()
        if name in AMBIENT_ALLOWLIST or name.startswith("LC_")
    }
    home = root / "home"
    if root.is_dir():
        home.mkdir(mode=0o700, exist_ok=True)
    result.update(
        {
            "HOME": str(home),
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
        require(row is not None, "check failed: row is not None")
        if row[0] is None:
            return
        target = conn.execute(
            "SELECT to_regclass('bakeoff_image_audit._pgaudit_probe')::oid"
        ).fetchone()
        require(target == (None,), "ambiguous image audit probe; refusing to replace either table")
        count = conn.execute("SELECT count(*) FROM public._pgaudit_probe").fetchone()
        conn.execute("CREATE SCHEMA IF NOT EXISTS bakeoff_image_audit")
        conn.execute("ALTER TABLE public._pgaudit_probe SET SCHEMA bakeoff_image_audit")
        require(
            conn.execute("SELECT to_regclass('bakeoff_image_audit._pgaudit_probe')::oid").fetchone()
            == row,
            "check failed: conn.execute(\"SELECT to_regclass('bakeoff_image_audit._pgaudit_probe')::oid\").fetchone() == row",
        )
        require(
            conn.execute("SELECT count(*) FROM bakeoff_image_audit._pgaudit_probe").fetchone()
            == count,
            "check failed: conn.execute('SELECT count(*) FROM bakeoff_image_audit._pgaudit_probe').fetchone() == count",
        )


def record_preparation(root: Path, revision: int, dropped_keys: list[str]) -> None:
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
            "dropped_config_keys": dropped_keys,
        },
    )


def runtime_python(root: Path) -> Path:
    # uv sync --frozen --project sources/gobby builds this gobby-sync environment.
    return root / "tools/gobby/venv/bin/python"


def run_runtime_gobby(
    root: Path, env: dict[str, str], operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Run one gobby step under the runtime interpreter, which verifies its own gobby first."""
    worker = Path(__file__).with_name("runtime_gobby.py")
    try:
        completed = subprocess.run(
            [str(runtime_python(root)), "-I", str(worker), operation, str(root)],
            input=json.dumps(payload),
            cwd=root / "sources/gobby",
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EvidenceError(f"runtime gobby {operation} could not run: {error}") from error
    require(
        completed.returncode == 0,
        f"runtime gobby {operation} exited {completed.returncode}: {completed.stderr.strip()}",
    )
    try:
        return json.loads(completed.stdout.splitlines()[-1])
    except (IndexError, ValueError) as error:
        raise EvidenceError(
            f"runtime gobby {operation} printed no result: {completed.stdout!r}"
        ) from error


def prepare(root: Path) -> None:
    embedding_readiness(EMBEDDING_API_BASE, EMBEDDING_MODEL)
    # These changes affect this isolated setup process only, never its parent session.
    clean = isolated_environment(root, dict(os.environ))
    for name in tuple(os.environ):
        if name not in clean:
            os.environ.pop(name)
    os.environ.update(clean)
    run_runtime_gobby(root, clean, "identity", {})

    credentials = _load_env_file(contained_file(root, "config/services.env"))
    dsn = (
        f"postgresql://{credentials['BAKEOFF_POSTGRES_USER']}:"
        f"{quote(credentials['BAKEOFF_POSTGRES_PASSWORD'], safe='')}@127.0.0.1:"
        f"{PORTS['postgres']}/{credentials['BAKEOFF_POSTGRES_DB']}"
    )
    parsed = urlparse(dsn)
    require(
        parsed.hostname == "127.0.0.1" and parsed.port == 61234,
        "check failed: parsed.hostname == '127.0.0.1' and parsed.port == 61234",
    )
    require(
        parsed.path == "/gobby_bakeoff_22948", "check failed: parsed.path == '/gobby_bakeoff_22948'"
    )
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
    result = run_runtime_gobby(
        root,
        clean,
        "prepare-daemon",
        {
            "dsn": dsn,
            "values": {
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
                **EMBEDDING_VALUES,
            },
            "falkordb_password": credentials["BAKEOFF_FALKORDB_PASSWORD"],
        },
    )
    record_preparation(root, result["revision"], result["dropped_keys"])
    print("isolated schema and config prepared; no daemon or model generation started")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-session", required=True)
    args = parser.parse_args()
    assert_owned_runtime(DEFAULT_RUNTIME_ROOT, args.owner_session)
    prepare(DEFAULT_RUNTIME_ROOT)


if __name__ == "__main__":
    main()
