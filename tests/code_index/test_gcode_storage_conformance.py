"""Conformance tests between the Rust gcode writer and Python code-index models."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from gobby.code_index.models import IndexedFile, Symbol
from gobby.code_index.storage import CodeIndexStorage
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.utils.native_bin import resolve_native_bin

pytestmark = pytest.mark.integration

FIXTURE_FILE = "pkg/sample.py"
INITIAL_SOURCE = '''\
def greet(name: str) -> str:
    """Return a greeting."""
    return f"Hello, {name}!"


class Worker:
    def run(self) -> str:
        return greet("worker")
'''
CHANGED_SOURCE = INITIAL_SOURCE.replace("Hello", "Hi")


def _run_gcode(gcode_bin: str, root: Path, env: dict[str, str], *args: str) -> None:
    result = subprocess.run(
        [gcode_bin, "--project", str(root), *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _write_fixture(root: Path, source: str) -> None:
    path = root / FIXTURE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def _project_id(root: Path) -> str:
    project_id = json.loads((root / ".gobby" / "gcode.json").read_text())["id"]
    assert isinstance(project_id, str)
    return project_id


def _symbol_row(
    code_storage: CodeIndexStorage,
    project_id: str,
    name: str,
) -> Symbol:
    row = code_storage.db.fetchone(
        """
        SELECT *
        FROM code_symbols
        WHERE project_id = %s AND file_path = %s AND name = %s
        """,
        (project_id, FIXTURE_FILE, name),
    )
    assert row is not None
    return Symbol.from_row(row)


def test_real_gcode_writer_matches_python_model_contract(
    tmp_path: Path,
    postgres_database_url: str,
    postgres_schema: str,
    request: pytest.FixtureRequest,
) -> None:
    """The production Rust writer and Python models share one storage contract."""
    gcode_bin = resolve_native_bin("gcode")
    if gcode_bin is None:
        pytest.skip("gcode binary is not installed")

    root = tmp_path / "repo"
    root.mkdir()
    _write_fixture(root, INITIAL_SOURCE)

    scoped_database_url = postgres_database_url + f"?options=-csearch_path%3D{postgres_schema}"
    code_db = PostgresHubDatabase(scoped_database_url)
    code_db.apply_migrations()
    request.addfinalizer(code_db.close)
    code_storage = CodeIndexStorage(code_db)

    env = os.environ.copy()
    gobby_home = tmp_path / "gobby-home"
    gobby_home.mkdir()
    bootstrap_path = gobby_home / "bootstrap.yaml"
    bootstrap_path.write_text(
        f"hub_backend: postgres\ndatabase_url: {scoped_database_url}\n",
        encoding="utf-8",
    )
    bootstrap_path.chmod(0o600)
    kek_path = gobby_home / ".secret_kek"
    kek_path.write_bytes(Fernet.generate_key())
    kek_path.chmod(0o600)
    env["GOBBY_HOME"] = str(gobby_home)
    env["DATABASE_URL"] = scoped_database_url
    env["GCODE_DATABASE_URL"] = scoped_database_url
    env["GOBBY_POSTGRES_DSN"] = scoped_database_url
    env.setdefault("GCODE_BROKER_TIMEOUT_MS", "1")

    _run_gcode(gcode_bin, root, env, "init", "--quiet")
    project_id = _project_id(root)
    request.addfinalizer(lambda: code_storage.delete_project_index(project_id))
    _run_gcode(gcode_bin, root, env, "index", "--full", "--quiet")

    greet = _symbol_row(code_storage, project_id, "greet")
    run = _symbol_row(code_storage, project_id, "run")
    indexed_file = code_storage.get_file(project_id, FIXTURE_FILE)
    assert indexed_file is not None

    greet_start = INITIAL_SOURCE.encode().index(b"def greet")
    assert greet.id == Symbol.make_id(project_id, FIXTURE_FILE, "greet", "function", greet_start)
    assert greet.qualified_name == "greet"
    assert run.qualified_name == "Worker.run"
    assert indexed_file.id == IndexedFile.make_id(project_id, FIXTURE_FILE)

    code_storage.update_symbol_summary(greet.id, greet.content_hash, "Greets a caller.")
    code_db.execute(
        """
        UPDATE code_indexed_files
        SET graph_synced = TRUE,
            vectors_synced = TRUE,
            graph_sync_attempted_at = NOW()
        WHERE id = %s
        """,
        (indexed_file.id,),
    )

    _write_fixture(root, CHANGED_SOURCE)
    _run_gcode(gcode_bin, root, env, "index", "--full", "--quiet")

    reindexed_greet = code_storage.get_symbol(greet.id)
    reindexed_file = code_storage.get_file(project_id, FIXTURE_FILE)
    assert reindexed_greet is not None
    assert reindexed_file is not None
    assert reindexed_greet.content_hash != greet.content_hash
    assert reindexed_greet.summary is None
    assert reindexed_file.graph_synced is False
    assert reindexed_file.vectors_synced is False
    assert reindexed_file.graph_sync_attempted_at is None
