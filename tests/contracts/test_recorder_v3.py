"""Tests for the v3 recorder: normalizers, seeds, DB deltas, captures, the guard and teardown.

Normalizer vectors are daemon-free. Seeds, deltas, captures and teardown use the isolated
test hub (DATABASE_URL), never a daemon. The guard refusals connect to nothing.
"""

from __future__ import annotations

import copy
import http.server
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from websockets.sync.server import ServerConnection, serve

from gobby.config.bootstrap import load_bootstrap
from gobby.runtime_grants.handshake import decode_grant_header, encode_grant_header
from gobby.runtime_grants.schema import GrantBundle
from gobby.utils.daemon_url import resolve_daemon_url
from scripts.parity_waivers import PASSED
from tests.contracts import corpus_v3, http_corpus, normalizers_v3, recorder_v3
from tests.contracts.http_corpus import CorpusError
from tests.contracts.recorder_v3_support import (
    ProviderEdgeCapture,
    send_mcp_rest,
)
from tests.contracts.recorder_v3_support import (
    RecorderError as SupportRecorderError,
)
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
def test_ws_publish_capture_subscribes_and_keeps_only_the_declared_types() -> None:
    """A fake that delivers as the daemon does: an event type reaches only its subscribers.

    ``session_event`` is an event type in the daemon's broadcast filter, so it is delivered only
    after a subscribe that names it. ``other`` is not an event type and passes to any subscriber.
    """
    subscribes: list[list[str]] = []

    def publish(connection: ServerConnection) -> None:
        subscribed: set[str] = set()
        for raw in connection:
            message = json.loads(raw)
            if message.get("type") != "subscribe":
                continue
            subscribed = set(message["events"])
            subscribes.append(list(message["events"]))
            connection.send(json.dumps({"type": "subscribe_success", "events": sorted(subscribed)}))
            for frame in (
                {"type": "session_event", "n": 1},
                {"type": "other"},
                {"type": "session_event", "n": 2},
            ):
                if frame["type"] == "session_event" and "session_event" not in subscribed:
                    continue
                connection.send(json.dumps(frame))

    with serve(publish, "127.0.0.1", 0) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.socket.getsockname()[1]
        with recorder_v3.ws_publish_capture(
            f"ws://127.0.0.1:{port}/", ["session_event"]
        ) as capture:
            captured = capture.drain()
    assert subscribes == [["session_event"]]
    assert captured == [{"type": "session_event", "n": 1}, {"type": "session_event", "n": 2}]


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


PER_RUN_URL = "http://127.0.0.1:61234"
PROBE_INVOCATION = {"server": "gobby", "tool": "list_tools", "arguments": {}}


class _Spawned(Exception):
    """Raised by a fake process launcher after it records the environment; nothing starts."""


@pytest.mark.unit
def test_stdio_sender_env_points_at_the_per_run_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper's environment names the per-run home, the fence and the per-run daemon URL.

    The fake Popen raises after recording the environment, so no process starts.
    """
    captured: dict[str, Any] = {}

    def fake_popen(argv: Any, **kwargs: Any) -> None:
        captured["env"] = kwargs["env"]
        raise _Spawned

    monkeypatch.setattr(
        recorder_v3, "subprocess", SimpleNamespace(PIPE=subprocess.PIPE, Popen=fake_popen)
    )
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(_Spawned):
        recorder_v3.send_mcp_stdio(
            PROBE_INVOCATION,
            home=home,
            cwd=tmp_path,
            scratch=tmp_path,
            daemon_url=PER_RUN_URL,
        )
    env = captured["env"]
    assert env.get("GOBBY_TEST_PROTECT") == "1"
    assert env.get("GOBBY_HOME") == str(home)
    assert env.get("GOBBY_DAEMON_URL") == PER_RUN_URL
    assert resolve_daemon_url(env=env) == PER_RUN_URL


@pytest.mark.unit
def test_cli_sender_env_points_at_the_per_run_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: Any, **kwargs: Any) -> None:
        captured["env"] = kwargs["env"]
        raise _Spawned

    monkeypatch.setattr(recorder_v3, "subprocess", SimpleNamespace(run=fake_run))
    with pytest.raises(_Spawned):
        recorder_v3.send_cli(
            ["schema", "--help"], stdin=None, scratch=tmp_path, daemon_url=PER_RUN_URL
        )
    env = captured["env"]
    assert env.get("GOBBY_TEST_PROTECT") == "1"
    assert env.get("GOBBY_HOME") == str(tmp_path / "home")
    assert env.get("GOBBY_DAEMON_URL") == PER_RUN_URL
    assert resolve_daemon_url(env=env) == PER_RUN_URL


OPERATOR_PORTS = (60887, 60888)


@pytest.mark.unit
@pytest.mark.parametrize(
    "sealed", [recorder_v3._stdio_env, recorder_v3._cli_env], ids=["stdio", "cli"]
)
def test_sealed_home_bootstrap_names_no_operator_port_and_loads(
    tmp_path: Path, sealed: Callable[[Path, str], dict[str, str]]
) -> None:
    """The sealed home's bootstrap.yaml is mode 0600, loads, and names no operator port.

    Without GOBBY_DAEMON_URL, a bootstrap reader in the sealed home resolves a port other than
    the operator's 60887 or 60888. The test does not dial it.
    """
    home = tmp_path / "home"
    env = sealed(home, PER_RUN_URL)
    path = home / "bootstrap.yaml"
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    bootstrap = load_bootstrap(str(path))
    assert bootstrap.daemon_port not in OPERATOR_PORTS
    assert bootstrap.websocket_port not in OPERATOR_PORTS
    assert bootstrap.database_url is None
    without_url = {key: value for key, value in env.items() if key != "GOBBY_DAEMON_URL"}
    assert resolve_daemon_url(path, env=without_url) == f"http://127.0.0.1:{bootstrap.daemon_port}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "sealed", [recorder_v3._stdio_env, recorder_v3._cli_env], ids=["stdio", "cli"]
)
def test_a_home_with_its_own_bootstrap_keeps_those_bytes(
    tmp_path: Path, sealed: Callable[[Path, str], dict[str, str]]
) -> None:
    """A per-run daemon's home already holds its bootstrap.yaml; sealing leaves it unchanged."""
    home = tmp_path / "home"
    home.mkdir()
    own = "daemon_port: 61234\nwebsocket_port: 61235\n"
    path = home / "bootstrap.yaml"
    path.write_text(own)
    path.chmod(0o600)
    sealed(home, PER_RUN_URL)
    assert path.read_text() == own


class _HealthDaemon:
    """An ephemeral HTTP listener that answers every GET 200 and records its paths."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self._lock = threading.Condition()
        paths, lock = self.paths, self._lock

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with lock:
                    paths.append(self.path)
                    lock.notify_all()
                body = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def wait_for(self, path: str, timeout: float) -> bool:
        """Block until a GET for ``path`` arrives or the timeout passes; True when it arrived."""
        with self._lock:
            return self._lock.wait_for(lambda: path in self.paths, timeout)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.mark.integration
def test_stdio_wrapper_health_probe_reaches_only_the_injected_daemon(tmp_path: Path) -> None:
    """The real wrapper, run with the sender's environment, probes the injected daemon only.

    A stand-in process whose argv names the per-run home and ``gobby.runner`` satisfies the
    fence, so the wrapper finds a running daemon and never reaches the start branch. The
    wrapper dials the ephemeral listener named by GOBBY_DAEMON_URL. The test observes only that
    listener; the sealed home's bootstrap.yaml is covered by its own test.
    """
    home = tmp_path / "home"
    home.mkdir()
    health = _HealthDaemon()
    try:
        env = recorder_v3._stdio_env(home, health.url)
        stand_in = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)", "gobby.runner", str(home)]
        )
        wrapper: subprocess.Popen[str] | None = None
        try:
            with (tmp_path / "wrapper-stderr.log").open("w") as stderr:
                wrapper = subprocess.Popen(
                    [sys.executable, "-m", "gobby.mcp_proxy.stdio"],
                    cwd=tmp_path,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    text=True,
                )
            assert wrapper.stdin is not None
            lines: queue.Queue[str | None] = queue.Queue()
            threading.Thread(
                target=recorder_v3._pump_lines, args=(wrapper.stdout, lines), daemon=True
            ).start()
            for message in (
                recorder_v3._initialize_message(),
                recorder_v3._initialized_message(),
                recorder_v3._call_message(PROBE_INVOCATION),
            ):
                wrapper.stdin.write(json.dumps(message) + "\n")
                wrapper.stdin.flush()
            replies: dict[int, dict[str, Any]] = {}
            while recorder_v3.MCP_CALL_ID not in replies:
                replies.update(recorder_v3._stdio_reply(lines))
            assert health.wait_for("/api/health", timeout=60.0)
        finally:
            if wrapper is not None:
                wrapper.kill()
                wrapper.wait()
            stand_in.kill()
            stand_in.wait()
    finally:
        health.close()


@pytest.mark.unit
def test_rehearsal_profile_is_refused_before_any_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rehearsal mode creates and sweeps nothing; the Python recorder only refuses it."""

    def no_sweep(url: str) -> list[str]:
        raise AssertionError("the recorder swept dead runs in rehearsal mode (H6)")

    monkeypatch.setattr(recorder_v3, "sweep_dead_runs", no_sweep)
    profile = tmp_path / "rehearsal.json"
    profile.write_text(
        json.dumps(
            {
                "gobby_home": str(tmp_path / "rehearsal-home"),
                "postgres_host": "127.0.0.1",
                "postgres_system_identifier": "7000000000000000001",
                "services": {"postgres": {"port": 61500}},
            }
        )
    )
    profile.chmod(0o600)
    url = "postgresql://gobby_test:gobby_test@127.0.0.1:61500/gobby_test"
    env = {"DATABASE_URL": url, "GOBBY_HUB_REHEARSAL_PROFILE": str(profile)}
    with (
        pytest.raises(recorder_v3.RecorderError, match="rehearsal"),
        recorder_v3.open_run_database(base_url=url, env=env),
    ):
        pass


def _record_case() -> dict[str, Any]:
    return _v3_case(
        db_delta=[
            {
                "table": "probe_rows",
                "key": ["id"],
                "columns": "all",
                "normalizers": {},
                "order": "set",
            }
        ],
        side_effects=[{"channel": "ws_publish", "match": {"types": ["probe"]}, "normalizers": {}}],
    )


@pytest.mark.unit
def test_secret_keyed_values_are_written_redacted_and_still_match_the_raw_replay() -> None:
    case = _record_case()
    observation = recorder_v3.Observation(
        response={"status": 200, "headers": {}, "body": None},
        db_delta=[
            {
                "added": [{"id": "gamma", "deployment_token": "raw-token"}],
                "removed": [],
                "changed": [],
            }
        ],
        side_effects=[[{"type": "probe", "signature": "raw-signature"}]],
    )
    recorder_v3._recorded_entries(case, observation)
    written = json.dumps(case)
    assert "raw-token" not in written
    assert "raw-signature" not in written
    assert case["db_delta"][0]["recorded"]["added"] == [
        {"id": "gamma", "deployment_token": "@secret@"}
    ]
    assert case["side_effects"][0]["recorded"] == [{"type": "probe", "signature": "@secret@"}]
    actual, observed = recorder_v3._classify_inputs(observation)
    result = http_corpus.classify_replay(case, actual, {}, observed=observed)
    assert result.outcome == PASSED


@pytest.mark.unit
def test_a_changed_secret_keyed_column_is_refused_at_record_time() -> None:
    case = _record_case()
    observation = recorder_v3.Observation(
        response={"status": 200, "headers": {}, "body": None},
        db_delta=[
            {
                "added": [],
                "removed": [],
                "changed": [
                    {
                        "key": {"id": "alpha"},
                        "column": "deployment_token",
                        "before": "old-token",
                        "after": "new-token",
                    }
                ],
            }
        ],
        side_effects=[[]],
    )
    with pytest.raises(recorder_v3.RecorderError, match="deployment_token"):
        recorder_v3._recorded_entries(case, observation)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("before", "after"),
    [({"token": "raw"}, "plain"), ("plain", {"token": "raw"})],
    ids=["before-carries-a-secret-key", "after-carries-a-secret-key"],
)
def test_a_changed_value_carrying_a_secret_key_is_refused_at_record_time(
    before: Any, after: Any
) -> None:
    """A non-secret column is refused when either side of the change holds a secret-keyed field."""
    case = _record_case()
    observation = recorder_v3.Observation(
        response={"status": 200, "headers": {}, "body": None},
        db_delta=[
            {
                "added": [],
                "removed": [],
                "changed": [
                    {
                        "key": {"id": "alpha"},
                        "column": "config",
                        "before": before,
                        "after": after,
                    }
                ],
            }
        ],
        side_effects=[[]],
    )
    with pytest.raises(recorder_v3.RecorderError, match="cannot be recorded"):
        recorder_v3._recorded_entries(case, observation)


@pytest.mark.unit
def test_redaction_leaves_a_committed_recording_unchanged() -> None:
    committed = json.loads((corpus_v3.V3_DIR / "config_values_patch.json").read_text())
    case = copy.deepcopy(committed)
    observation = recorder_v3.Observation(
        response=case["response"],
        db_delta=[entry["recorded"] for entry in case["db_delta"]],
        side_effects=[entry["recorded"] for entry in case["side_effects"]],
    )
    recorder_v3._recorded_entries(case, observation)
    assert case == committed


def _client(status: int, body: Any, seen: list[httpx.Request] | None = None) -> httpx.Client:
    """An httpx client answering every request with status and body, without leaving the process."""

    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(answer))


@pytest.mark.unit
def test_seed_http_step_refuses_a_non_success_answer() -> None:
    step = {
        "method": "POST",
        "path": "/api/comms/send",
        "credential": "none",
        "body": {"channel_name": "corpus-slack", "content": "corpus seed message"},
    }
    recorder_v3.send_seed_http(_client(200, {"status": "sent"}), step, operator={})
    with pytest.raises(recorder_v3.RecorderError):
        recorder_v3.send_seed_http(_client(500, {"detail": "boom"}), step, operator={})


@pytest.mark.unit
def test_rest_seed_step_refuses_a_success_false_answer() -> None:
    step = {
        "method": "POST",
        "path": "/api/mcp/gobby-voice/tools/add_vocab",
        "credential": "none",
        "body": {"terms": "corpus-term-one, corpus-term-two"},
    }
    recorder_v3.send_seed_http(_client(200, {"success": True, "added": 2}), step, operator={})
    with pytest.raises(recorder_v3.RecorderError):
        recorder_v3.send_seed_http(
            _client(200, {"success": False, "error": "refused"}), step, operator={}
        )


@pytest.mark.unit
def test_mcp_rest_sender_requires_http_200() -> None:
    invocation = {
        "server": "gobby-communications",
        "tool": "remove_channel",
        "arguments": {"name": "corpus-slack"},
    }
    body = {"success": True, "channels": [{"name": "corpus-slack"}]}
    seen: list[httpx.Request] = []
    assert send_mcp_rest(_client(200, body, seen), invocation, operator={}) == body
    assert [(request.method, request.url.path) for request in seen] == [
        ("POST", "/api/mcp/gobby-communications/tools/remove_channel")
    ]
    assert json.loads(seen[0].content) == {"name": "corpus-slack"}
    with pytest.raises(SupportRecorderError):
        send_mcp_rest(_client(500, {"detail": "boom"}), invocation, operator={})


@pytest.mark.unit
def test_mcp_recording_carries_the_case_masks() -> None:
    case = _v3_case(
        kind="mcp",
        ledger_key="MCP gobby-communications:list_channels",
        invocation={
            "transport": "rest",
            "server": "gobby-communications",
            "tool": "list_channels",
            "arguments": {},
        },
        mask=["/response/result/checked_at"],
    )
    del case["request"]
    raw = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"checked_at": "2026-11-19T06:00:00Z", "channels": []},
    }
    normalized = recorder_v3._normalize_response(case, raw, WINDOW)
    assert normalized["result"]["channels"] == [], "the unmasked field survives"
    assert normalized["result"]["checked_at"] == http_corpus.MASK


@pytest.mark.unit
def test_recorded_db_values_carry_the_case_masks() -> None:
    entry = {
        "table": "comms_channels",
        "key": ["id"],
        "columns": "all",
        "normalizers": {},
        "order": "set",
    }
    case = _v3_case(db_delta=[entry], mask=["/db_delta/0/recorded/added/0/id"])
    observation = recorder_v3.Observation(
        response={"status": 200, "headers": {}, "body": None},
        db_delta=[
            {
                "added": [{"id": "5b0c7a1e-3f2d-4c8a-9e61-0c0a0c0a0001", "name": "corpus-slack"}],
                "removed": [],
                "changed": [],
            }
        ],
    )
    recorder_v3._recorded_entries(case, observation)
    assert case["db_delta"][0]["recorded"]["added"][0]["name"] == "corpus-slack"
    assert case["db_delta"][0]["recorded"]["added"][0]["id"] == http_corpus.MASK


@pytest.mark.unit
def test_each_template_records_in_its_own_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    for name in ("alpha", "beta", "gamma"):
        (templates / f"{name}.json").write_text(json.dumps({"name": name}))
    out = tmp_path / "out"
    out.mkdir()
    opened: list[str] = []

    @contextmanager
    def counting_run(**_: Any) -> Iterator[SimpleNamespace]:
        opened.append("run")
        yield SimpleNamespace(
            name=f"run-{len(opened)}", url="postgresql://run", base_url="postgresql://base"
        )

    @contextmanager
    def counting_daemon(**_: Any) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(operator_headers=lambda: {})

    def record(run: Any, daemon: Any, template: Mapping[str, Any], **_: Any) -> dict[str, Any]:
        return {"name": template["name"]}

    monkeypatch.setattr(recorder_v3, "open_run_database", counting_run)
    monkeypatch.setattr(recorder_v3, "recorder_daemon", counting_daemon)
    monkeypatch.setattr(recorder_v3, "record_template", record)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_: str(tmp_path / "scratch"))
    assert recorder_v3.main(["--templates", str(templates), "--out", str(out)]) == 0
    assert sorted(path.name for path in out.iterdir()) == ["alpha.json", "beta.json", "gamma.json"]
    assert len(opened) == 3, "each template opens its own run"


class _EdgeLog:
    """A stand-in for the provider edge's arrival log: arrivals in order, cleared on demand."""

    def __init__(self) -> None:
        self._arrivals: list[dict[str, Any]] = []

    def add(self, arrival: dict[str, Any]) -> None:
        self._arrivals.append(arrival)

    def hits(self) -> list[dict[str, Any]]:
        return list(self._arrivals)

    def clear(self) -> None:
        self._arrivals.clear()


@pytest.mark.unit
def test_provider_edge_capture_keeps_listed_hosts_in_arrival_order() -> None:
    edge = _EdgeLog()
    capture = ProviderEdgeCapture(edge, hosts=["slack.com"])
    edge.add(
        {
            "host": "slack.com",
            "method": "POST",
            "path": "/api/chat.postMessage",
            "headers": {"authorization": "Bearer test-token"},
            "body": {"text": "corpus message one"},
        }
    )
    edge.add(
        {"host": "other.test", "method": "GET", "path": "/ignored", "headers": {}, "body": None}
    )
    edge.add(
        {
            "host": "slack.com",
            "method": "GET",
            "path": "/api/auth.test",
            "headers": {},
            "body": None,
        }
    )
    assert len(edge.hits()) == 3, "the stub edge log holds the three arrivals"
    assert capture.hits() == [
        {"host": "slack.com", "method": "POST", "path": "/api/chat.postMessage"},
        {"host": "slack.com", "method": "GET", "path": "/api/auth.test"},
    ]
