"""Conformance tests between the Rust gcode writer and Python code-index models."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from gobby.code_index.models import IndexedFile, Symbol
from gobby.code_index.storage import CodeIndexStorage
from gobby.runtime_grants.launch import write_grant_file
from gobby.runtime_grants.schema import (
    AIUnavailableCapability,
    GrantBundle,
    GrantCapabilities,
    GrantDeployment,
    GrantPrincipal,
    PostgresDirect,
    SchemaIdentity,
    UnavailableCapability,
)
from gobby.runtime_grants.signing import payload_checksum
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.schema_contract import expected_schema_identity
from gobby.utils.machine_id import get_machine_id
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


def _managed_grant(*, project_id: str, machine_id: str, dsn: str) -> GrantBundle:
    now = int(time.time())
    unsigned = GrantBundle(
        config_revision=1,
        deployment=GrantDeployment(token="cafebabedeadbeef", fencing_epoch=1),
        schema_identity=SchemaIdentity.model_validate(expected_schema_identity()),
        principal=GrantPrincipal(
            kind="agent_run",
            machine_id=machine_id,
            project_id=project_id,
            execution_id="grant-fixture",
            session_id="grant-fixture",
        ),
        capabilities=GrantCapabilities(
            postgres=PostgresDirect(
                dsn=dsn,
                role_name="gobby_grant_fixture",
                credential_generation=1,
                valid_until=now + 86_400,
            ),
            falkordb=UnavailableCapability(),
            qdrant=UnavailableCapability(),
            embed=AIUnavailableCapability(),
            text_generate=AIUnavailableCapability(),
            tool_chat=AIUnavailableCapability(),
            vision_extract=AIUnavailableCapability(),
            audio_transcribe=AIUnavailableCapability(),
            broker_operations=(),
        ),
        issued_at=now,
        expires_at=now + 86_400,
    )
    checksum = payload_checksum(unsigned)
    return unsigned.model_copy(update={"payload_checksum": checksum})


ROOT = Path(__file__).resolve().parents[2]


def _gcode_bin() -> str:
    debug = ROOT / "target" / "debug" / "gcode"
    if debug.is_file():
        return str(debug)
    found = resolve_native_bin("gcode")
    if found is None:
        pytest.skip("gcode binary is not installed")
    return found


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
    gobby = root / ".gobby"
    for name in ("project.json", "gcode.json"):
        path = gobby / name
        if path.is_file():
            project_id = json.loads(path.read_text())["id"]
            assert isinstance(project_id, str)
            return project_id
    raise AssertionError("project identity file is missing")


def _symbol_row(
    code_storage: CodeIndexStorage,
    project_id: str,
    name: str,
) -> Symbol:
    matches = [
        symbol
        for symbol in code_storage.get_symbols_for_file(project_id, FIXTURE_FILE)
        if symbol.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_real_gcode_writer_matches_python_model_contract(
    tmp_path: Path,
    postgres_database_url: str,
    postgres_schema: str,
    request: pytest.FixtureRequest,
) -> None:
    """The production Rust writer and Python models share one storage contract."""
    gcode_bin = _gcode_bin()

    root = tmp_path / "repo"
    root.mkdir()
    _write_fixture(root, INITIAL_SOURCE)

    separator = "&" if "?" in postgres_database_url else "?"
    scoped_database_url = (
        postgres_database_url + f"{separator}options=-csearch_path%3D{postgres_schema}"
    )
    code_db = PostgresHubDatabase(scoped_database_url)
    code_db.apply_migrations()
    request.addfinalizer(code_db.close)
    code_storage = CodeIndexStorage(code_db)

    env = os.environ.copy()
    gobby_home = tmp_path / "gobby-home"
    gobby_home.mkdir()
    machine_id = get_machine_id()
    assert machine_id is not None
    from tests.fixtures.postgres import (
        TEST_USER_EMAIL,
        TEST_USER_ID,
        TEST_USER_NAME,
        TEST_USER_PASSWORD_HASH,
    )

    code_db.execute(
        """
        INSERT INTO users (id, email, name, password_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (TEST_USER_ID, TEST_USER_EMAIL, TEST_USER_NAME, TEST_USER_PASSWORD_HASH),
    )
    code_db.execute(
        """
        INSERT INTO machines (id, owner_user_id)
        VALUES (%s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (machine_id, TEST_USER_ID),
    )
    (gobby_home / "machine_id").write_text(machine_id, encoding="utf-8")
    env["GOBBY_HOME"] = str(gobby_home)
    env["DATABASE_URL"] = scoped_database_url
    env.setdefault("GCODE_BROKER_TIMEOUT_MS", "1")

    project_meta = root / ".gobby"
    project_meta.mkdir(parents=True, exist_ok=True)
    project_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    (project_meta / "project.json").write_text(
        json.dumps({"id": project_id, "name": "storage-conformance"})
    )
    grant = _managed_grant(
        project_id=project_id,
        machine_id=machine_id,
        dsn=scoped_database_url,
    )
    grant_path = write_grant_file(gobby_home / "grants" / "grant.json", grant)
    env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] = str(grant_path)

    _run_gcode(gcode_bin, root, env, "init", "--quiet")
    project_id = _project_id(root)
    grant = _managed_grant(
        project_id=project_id,
        machine_id=machine_id,
        dsn=scoped_database_url,
    )
    grant_path = write_grant_file(gobby_home / "grants" / "grant.json", grant)
    env["GOBBY_MANAGED_EXECUTION_BOOTSTRAP"] = str(grant_path)
    request.addfinalizer(lambda: code_storage.delete_project_index(project_id))
    _run_gcode(gcode_bin, root, env, "index", "--full", "--quiet")

    greet = _symbol_row(code_storage, project_id, "greet")
    run = _symbol_row(code_storage, project_id, "run")
    indexed_file = code_storage.get_file(project_id, FIXTURE_FILE)
    assert indexed_file is not None

    greet_start = INITIAL_SOURCE.encode().index(b"def greet")
    assert greet.id == Symbol.make_id(
        project_id,
        FIXTURE_FILE,
        indexed_file.content_hash,
        "greet",
        "function",
        greet_start,
    )
    assert greet.qualified_name == "greet"
    assert run.qualified_name == "Worker.run"
    assert indexed_file.id == IndexedFile.make_id(
        project_id, FIXTURE_FILE, indexed_file.content_hash
    )

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

    reindexed_greet = _symbol_row(code_storage, project_id, "greet")
    reindexed_file = code_storage.get_file(project_id, FIXTURE_FILE)
    assert reindexed_file is not None
    assert reindexed_greet.id != greet.id
    assert reindexed_greet.content_hash != greet.content_hash
    assert reindexed_greet.summary is None
    assert reindexed_file.graph_synced is False
    assert reindexed_file.vectors_synced is False
    assert reindexed_file.graph_sync_attempted_at is None
