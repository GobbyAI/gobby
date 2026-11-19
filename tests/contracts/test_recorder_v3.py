"""Tests for the v3 recorder: normalizers, seeds, DB deltas, captures, the guard and teardown.

Normalizer vectors are daemon-free. Seeds, deltas, captures and teardown use the isolated
test hub (DATABASE_URL), never a daemon. The guard refusals connect to nothing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from websockets.sync.server import ServerConnection, serve

from gobby.runtime_grants.handshake import decode_grant_header, encode_grant_header
from gobby.runtime_grants.schema import GrantBundle
from tests.contracts import corpus_v3, normalizers_v3, recorder_v3
from tests.contracts.http_corpus import CorpusError
from tests.fixtures.hub_guard import HARNESS_DATABASE_PATTERN, HubGuardError, maintenance_url

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "normalizer_vectors.json").read_text()
)
LIVE_HUB_URL = "postgresql://gobby:gobby@localhost:60891/gobby"
TEST_HUB_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
WINDOW = normalizers_v3.Window(
    start=datetime.fromisoformat(VECTORS["window"]["start"]),
    end=datetime.fromisoformat(VECTORS["window"]["end"]),
)


def _run_vector(vector: Mapping[str, Any]) -> Any:
    """Normalize one vector's input by its kind, raising NormalizationError as the recorder would."""
    kind = vector["kind"]
    if kind == "rows":
        return normalizers_v3.normalize_rows(
            vector["rows"],
            key=vector["key"],
            order=vector["order"],
            normalizers=vector["normalizers"],
            window=WINDOW,
        )
    if kind == "captures":
        return normalizers_v3.normalize_captures(
            vector["items"], vector["normalizers"], window=WINDOW
        )
    if kind == "document":
        return normalizers_v3.normalize_document(
            vector["document"], vector["normalizers"], window=WINDOW
        )
    raise AssertionError(f"{vector['id']}: unknown vector kind {kind!r}")


@pytest.mark.unit
@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda vector: vector["id"])
def test_normalizer_vector(vector: dict[str, Any]) -> None:
    if vector["expect"] == "pass":
        assert _run_vector(vector) == vector["normalized"]
        return
    with pytest.raises(normalizers_v3.NormalizationError, match=re.escape(vector["error"])):
        _run_vector(vector)


@pytest.mark.unit
def test_vectors_are_ordered_and_unique() -> None:
    ids = [vector["id"] for vector in VECTORS["vectors"]]
    assert ids == sorted(set(ids))
    assert {vector["kind"] for vector in VECTORS["vectors"]} == {"rows", "captures", "document"}


def _v3_case(**fields: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "schema_version": 3,
        "name": "probe_rows_case",
        "kind": "http",
        "family": "probe",
        "backend": "up",
        "credential": "none",
        "ledger_key": "HTTP GET /api/probe",
        "request": {
            "method": "GET",
            "path": "/api/probe",
            "query": {},
            "headers": {},
            "body": None,
        },
        "response": {"status": 200, "headers": {}, "body": None},
        "mask": [],
    }
    case.update(fields)
    rank = {key: index for index, key in enumerate(corpus_v3.V3_KEY_ORDER)}
    return dict(sorted(case.items(), key=lambda item: rank.get(item[0], len(rank))))


def _load_one(tmp_path: Path, case: dict[str, Any]) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "cases": [f"{case['name']}.json"],
                "families": {"probe": {"parity": "proxy", "origin": "python"}},
            }
        )
    )
    (tmp_path / f"{case['name']}.json").write_text(json.dumps(case))
    corpus_v3.load_cases(tmp_path)


@pytest.mark.unit
def test_set_order_without_a_key_is_a_loader_error(tmp_path: Path) -> None:
    entry = {"table": "probe_rows", "columns": "all", "normalizers": {}, "order": "set"}
    with pytest.raises(CorpusError, match="key is required"):
        _load_one(tmp_path, _v3_case(db_delta=[entry]))


@pytest.mark.unit
def test_absent_order_is_a_loader_error(tmp_path: Path) -> None:
    entry = {"table": "probe_rows", "key": ["id"], "columns": "all", "normalizers": {}}
    with pytest.raises(CorpusError, match="order is required"):
        _load_one(tmp_path, _v3_case(db_delta=[entry]))


@pytest.fixture
def hub_run() -> Iterator[recorder_v3.RunDatabase]:
    """A per-run database on the isolated test hub, dropped at teardown."""
    with recorder_v3.open_run_database(base_url=TEST_HUB_URL) as run:
        yield run


def _database_exists(name: str) -> bool:
    with psycopg.connect(maintenance_url(TEST_HUB_URL), autocommit=True) as connection:
        row = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row is not None


def _create_database(name: str) -> None:
    with psycopg.connect(maintenance_url(TEST_HUB_URL), autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')


def _drop_database(name: str) -> None:
    with psycopg.connect(maintenance_url(TEST_HUB_URL), autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _dead_pid() -> int:
    """Return the pid of a process that has exited, so it cannot be alive now."""
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(done.stdout.strip())


@pytest.mark.integration
@pytest.mark.parametrize(
    "url",
    [
        LIVE_HUB_URL,
        "postgresql://gobby_test:gobby_test@10.0.0.5:60892/gobby_test",
        "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test?dbname=gobby",
    ],
    ids=["live-hub", "non-loopback-host", "dbname-query-override"],
)
def test_recorder_refuses_a_database_url_that_fails_the_guard(url: str) -> None:
    with pytest.raises(HubGuardError), recorder_v3.open_run_database(base_url=url):
        pass


@pytest.mark.unit
def test_recorder_refuses_a_redirecting_libpq_environment() -> None:
    with (
        pytest.raises(HubGuardError, match="^R2"),
        recorder_v3.open_run_database(
            base_url=TEST_HUB_URL, env={"PGHOSTADDR": "10.0.0.5", "DATABASE_URL": TEST_HUB_URL}
        ),
    ):
        pass


@pytest.mark.integration
def test_per_run_database_is_named_and_dropped_after_teardown() -> None:
    with recorder_v3.open_run_database(base_url=TEST_HUB_URL) as run:
        name = run.name
        assert HARNESS_DATABASE_PATTERN.fullmatch(name) is not None
        assert _database_exists(name)
    assert not _database_exists(name)


@pytest.mark.integration
def test_dead_pid_database_is_swept_at_the_next_start() -> None:
    dead = f"gobby_harness_{_dead_pid()}_{uuid4().hex[:12]}"
    _create_database(dead)
    try:
        with recorder_v3.open_run_database(base_url=TEST_HUB_URL):
            assert not _database_exists(dead)
    finally:
        _drop_database(dead)


@pytest.mark.integration
def test_live_pid_database_is_kept_by_the_sweep() -> None:
    live = f"gobby_harness_{os.getpid()}_{uuid4().hex[:12]}"
    _create_database(live)
    try:
        assert recorder_v3.sweep_dead_runs(TEST_HUB_URL) == []
        assert _database_exists(live)
    finally:
        _drop_database(live)


@pytest.mark.integration
def test_seed_steps_run_in_order_before_the_snapshot(
    hub_run: recorder_v3.RunDatabase, tmp_path: Path
) -> None:
    (tmp_path / "create.sql").write_text(
        "CREATE TABLE probe_rows (id text PRIMARY KEY, note text);\n"
        "INSERT INTO probe_rows VALUES ('alpha', 'one');\n"
    )
    (tmp_path / "update.sql").write_text("UPDATE probe_rows SET note = 'two' WHERE id = 'alpha';\n")
    recorder_v3.apply_seed(
        hub_run, [{"sql": "create.sql"}, {"sql": "update.sql"}], seed_dir=tmp_path
    )
    with hub_run.connect() as connection:
        assert connection.execute("SELECT note FROM probe_rows").fetchall() == [("two",)]


@pytest.mark.integration
def test_failing_seed_step_fails_the_run(hub_run: recorder_v3.RunDatabase, tmp_path: Path) -> None:
    (tmp_path / "broken.sql").write_text("SELECT * FROM no_such_table;\n")
    with pytest.raises(recorder_v3.RecorderError):
        recorder_v3.apply_seed(hub_run, [{"sql": "broken.sql"}], seed_dir=tmp_path)


@pytest.mark.integration
def test_db_delta_reports_one_changed_column_one_removed_row_and_one_added_row(
    hub_run: recorder_v3.RunDatabase,
) -> None:
    with hub_run.connect() as connection:
        connection.execute("CREATE TABLE probe_rows (id text PRIMARY KEY, note text)")
        connection.execute("INSERT INTO probe_rows VALUES ('alpha', 'one'), ('beta', 'two')")
    entry = {
        "table": "probe_rows",
        "key": ["id"],
        "columns": "all",
        "normalizers": {},
        "order": "set",
    }
    before = recorder_v3.snapshot_table(hub_run, entry)
    with hub_run.connect() as connection:
        connection.execute("UPDATE probe_rows SET note = 'three' WHERE id = 'alpha'")
        connection.execute("DELETE FROM probe_rows WHERE id = 'beta'")
        connection.execute("INSERT INTO probe_rows VALUES ('gamma', 'four')")
    after = recorder_v3.snapshot_table(hub_run, entry)
    assert recorder_v3.table_delta(before, after, entry, window=WINDOW) == {
        "added": [{"id": "gamma", "note": "four"}],
        "removed": [{"id": "beta", "note": "two"}],
        "changed": [{"key": {"id": "alpha"}, "column": "note", "before": "one", "after": "three"}],
    }


@pytest.mark.integration
def test_pg_notify_capture_keeps_listened_channels_in_arrival_order(
    hub_run: recorder_v3.RunDatabase,
) -> None:
    with recorder_v3.pg_notify_capture(hub_run, ["probe_channel"]) as capture:
        with hub_run.connect() as connection:
            connection.execute("SELECT pg_notify('probe_channel', 'first')")
            connection.execute("SELECT pg_notify('other_channel', 'ignored')")
            connection.execute("SELECT pg_notify('probe_channel', 'second')")
        captured = capture.drain()
    assert captured == [
        {"channel": "probe_channel", "payload": "first"},
        {"channel": "probe_channel", "payload": "second"},
    ]


@pytest.mark.unit
def test_ws_publish_capture_keeps_only_the_declared_types() -> None:
    def publish(connection: ServerConnection) -> None:
        connection.send(json.dumps({"type": "probe", "n": 1}))
        connection.send(json.dumps({"type": "other"}))
        connection.send(json.dumps({"type": "probe", "n": 2}))

    with serve(publish, "127.0.0.1", 0) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.socket.getsockname()[1]
        with recorder_v3.ws_publish_capture(f"ws://127.0.0.1:{port}/", ["probe"]) as capture:
            captured = capture.drain()
    assert captured == [{"type": "probe", "n": 1}, {"type": "probe", "n": 2}]


@pytest.mark.unit
def test_grant_encoding_vector_is_byte_stable() -> None:
    """The runtime grant header that Python encodes is pinned; the Rust encoder must match it."""
    vector = json.loads((corpus_v3.V3_DIR / "grant_encoding.json").read_text())
    grant = GrantBundle.model_validate(vector["grant"])
    assert encode_grant_header(grant) == vector["header"]
    assert decode_grant_header(vector["header"]) == grant


@pytest.mark.unit
def test_files_home_capture_reports_created_modified_and_removed_paths(tmp_path: Path) -> None:
    (tmp_path / "changed.txt").write_text("before")
    (tmp_path / "gone.txt").write_text("gone")
    (tmp_path / "ignored.bin").write_text("untouched")
    before = recorder_v3.snapshot_files(tmp_path, "*.txt")
    (tmp_path / "changed.txt").write_text("after")
    (tmp_path / "gone.txt").unlink()
    (tmp_path / "new.txt").write_text("new")
    (tmp_path / "ignored.bin").write_text("changed but outside the glob")
    after = recorder_v3.snapshot_files(tmp_path, "*.txt")
    assert recorder_v3.files_delta(before, after) == {
        "created": ["new.txt"],
        "modified": ["changed.txt"],
        "removed": ["gone.txt"],
    }
