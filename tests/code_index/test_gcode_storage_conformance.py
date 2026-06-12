"""Conformance tests between the Rust gcode writer and Python code-index models."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from gobby.code_index.models import IndexedFile, Symbol
from gobby.code_index.storage import CodeIndexStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.native_bin import resolve_native_bin

pytestmark = pytest.mark.unit

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


def _scoped_database_url(database_url: str, schema: str) -> str:
    separator = "&" if "?" in database_url else "?"
    return f"{database_url}{separator}options=-csearch_path%3D{quote(schema)}"


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
    return json.loads((root / ".gobby" / "gcode.json").read_text())["id"]


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
    code_storage: CodeIndexStorage,
    code_db: HubDatabase,
) -> None:
    """The production Rust writer and Python models share one storage contract."""
    gcode_bin = resolve_native_bin("gcode")
    if gcode_bin is None:
        pytest.skip("gcode binary is not installed")

    root = tmp_path / "repo"
    root.mkdir()
    _write_fixture(root, INITIAL_SOURCE)

    env = os.environ.copy()
    env["GCODE_DATABASE_URL"] = _scoped_database_url(postgres_database_url, postgres_schema)
    env.setdefault("GCODE_BROKER_TIMEOUT_MS", "1")

    _run_gcode(gcode_bin, root, env, "init", "--quiet")
    project_id = _project_id(root)
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

    code_storage.update_symbol_summary(greet.id, "Greets a caller.")
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
