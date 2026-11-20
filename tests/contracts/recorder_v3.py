"""Recorder and replay driver for the schema 3 corpus over an isolated Python daemon.

Guard first: ``open_run_database`` refuses any URL that fails the hub guard, creates a
``gobby_harness_<pid>_<hex>`` database on the test hub, sweeps dead-pid runs, and drops the
database at teardown. ``recorder_daemon_v3`` starts the daemon on that database.

Per case, in order: seeds run (not asserted), the before-snapshot of every db_delta table is
taken, capture listeners open, the request is sent, captures drain, the after-snapshot is taken.
Every observed value is normalized by normalizers_v3 before it is compared or recorded.

Run as a module to record: ``uv run --no-sync python -m tests.contracts.recorder_v3``. It reads
the unrecorded templates in ``tests/contracts/v3/templates`` raw and writes the recorded cases to
``tests/contracts/v3``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg import sql
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from scripts.parity_waivers import Waiver
from tests.contracts import corpus_v3, http_corpus
from tests.contracts.comms.fake_edge import FakeEdge
from tests.contracts.http_corpus import CorpusError
from tests.contracts.normalizers_v3 import (
    Window,
    json_form,
    normalize_captures,
    normalize_document,
    normalize_images,
    unordered_key,
)
from tests.contracts.recorder_daemon_v3 import recorder_daemon
from tests.contracts.recorder_v3_support import (
    CAPTURE_CAP_SECONDS,
    CAPTURE_QUIET_SECONDS,
    ProviderEdgeCapture,
    ProviderEdgeLog,
    recorded_db_delta,
    recorder_credential_headers,
    refuse_pending_captures,
    send_mcp_rest,
)
from tests.contracts.recorder_v3_support import RecorderError as RecorderError
from tests.contracts.recorder_v3_support import send_seed_http as send_seed_http
from tests.e2e import conftest as e2e
from tests.fixtures.hub_guard import (
    HARNESS_DATABASE_PATTERN,
    GuardMode,
    HubGuard,
    check_derived_url,
    check_test_hub,
    derive_run_url,
    harness_database_name,
    maintenance_url,
    sweep_dead_pid,
)

V3_DIR = corpus_v3.V3_DIR
TEMPLATE_DIR = V3_DIR / "templates"
SEED_DIR = V3_DIR / "seeds"
REPO_ROOT = Path(__file__).resolve().parents[2]
GLOB_ALL = "*"


@dataclass(frozen=True)
class RunDatabase:
    """One per-run database: its name, its derived URL, and the guarded base it derives from."""

    name: str
    url: str
    base_url: str

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection[Any]]:
        with psycopg.connect(self.url, autocommit=True) as connection:
            yield connection


def sweep_dead_runs(admin_base_url: str) -> list[str]:
    """Drop every harness database whose pid is dead; keep the live ones and other names."""
    return sweep_dead_pid(maintenance_url(admin_base_url), pattern=HARNESS_DATABASE_PATTERN)


@contextmanager
def open_run_database(
    *, base_url: str | None = None, env: Mapping[str, str] | None = None
) -> Iterator[RunDatabase]:
    """Create this run's database after the guard accepts the base URL; drop it on exit."""
    source: Mapping[str, str] = os.environ if env is None else env
    url = base_url if base_url is not None else source.get("DATABASE_URL")
    guard: HubGuard = check_test_hub(url, source)
    assert url is not None
    if guard.mode is GuardMode.REHEARSAL:
        # A rehearsal run creates and drops nothing and sweeps nothing. The Python recorder
        # has no rehearsal path, so it refuses before the sweep and before any connection.
        raise RecorderError("rehearsal profiles are not built on the Python recorder (H6)")
    sweep_dead_runs(url)
    name = harness_database_name(os.getpid(), secrets.token_hex(6))
    derived = derive_run_url(guard, name)
    check_derived_url(derived, name)
    admin = maintenance_url(url)
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(derived, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
        yield RunDatabase(name=name, url=derived, base_url=url)
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


# Seeds: ordered, applied before the before-snapshot, results not asserted.


def apply_seed(
    run: RunDatabase,
    steps: Sequence[Mapping[str, Any]],
    *,
    seed_dir: Path = SEED_DIR,
    send_http: Callable[[Mapping[str, Any]], None] | None = None,
) -> None:
    """Apply one case's seed steps in order; a failing step fails the run."""
    for index, step in enumerate(steps):
        if "sql" in step:
            text = (seed_dir / step["sql"]).read_text()
            try:
                with psycopg.connect(run.url) as connection:
                    connection.execute(text)
                    connection.commit()
            except psycopg.Error as error:
                raise RecorderError(f"seed step {index} ({step['sql']}) failed: {error}") from error
        elif "http" in step:
            if send_http is None:
                raise RecorderError(f"seed step {index} is an http step with no sender")
            send_http(step["http"])
        else:
            raise RecorderError(f"seed step {index} is neither sql nor http")


# DB deltas: a snapshot per entry, then a diff over normalized images.


def _columns(connection: psycopg.Connection[Any], entry: Mapping[str, Any]) -> list[str]:
    requested = entry["columns"]
    if requested != "all":
        return list(requested)
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
        (entry["table"],),
    ).fetchall()
    return [row[0] for row in rows]


def snapshot_table(run: RunDatabase, entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the table's selected columns as JSON-form rows, in the entry's snapshot order."""
    with run.connect() as connection:
        columns = _columns(connection, entry)
        order_by = list(entry.get("order_by") or entry["key"])
        query = sql.SQL("SELECT {columns} FROM {table} ORDER BY {order}").format(
            columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            table=sql.Identifier(entry["table"]),
            order=sql.SQL(", ").join(sql.Identifier(column) for column in order_by),
        )
        rows = connection.execute(query).fetchall()
    return [json_form(dict(zip(columns, row, strict=True))) for row in rows]


def _index(rows: Sequence[Mapping[str, Any]], key: Sequence[str]) -> dict[tuple[str, ...], Any]:
    indexed: dict[tuple[str, ...], Any] = {}
    for row in rows:
        identity = tuple(json.dumps(row[column], sort_keys=True) for column in key)
        if identity in indexed:
            raise RecorderError(f"key {list(key)} is not unique in {identity}")
        indexed[identity] = row
    return indexed


def table_delta(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    entry: Mapping[str, Any],
    *,
    window: Window,
) -> dict[str, Any]:
    """Return the entry's normalized delta: added, removed and changed (column level).

    The images of one entry are normalized as one unit, in the order added, removed, changed
    before, changed after, so an identifier that recurs keeps one ordinal. A changed column is
    reported only when its normalized values differ.
    """
    key = list(entry["key"])
    before_by, after_by = _index(before, key), _index(after, key)
    added = [after_by[identity] for identity in sorted(after_by.keys() - before_by.keys())]
    removed = [before_by[identity] for identity in sorted(before_by.keys() - after_by.keys())]
    pairs = [
        (before_by[identity], after_by[identity])
        for identity in sorted(before_by.keys() & after_by.keys())
        if before_by[identity] != after_by[identity]
    ]
    images = [
        *added,
        *removed,
        *(pair[0] for pair in pairs),
        *(pair[1] for pair in pairs),
    ]
    normalized = normalize_images(
        images,
        order=entry["order"],
        normalizers=entry.get("normalizers", {}),
        window=window,
    )
    cut = len(added) + len(removed)
    normalized_pairs = list(
        zip(normalized[cut : cut + len(pairs)], normalized[cut + len(pairs) :], strict=True)
    )
    changed: list[dict[str, Any]] = []
    for before_row, after_row in normalized_pairs:
        for column in sorted(before_row):
            if before_row[column] != after_row[column]:
                changed.append(
                    {
                        "key": {name: before_row[name] for name in key},
                        "column": column,
                        "before": before_row[column],
                        "after": after_row[column],
                    }
                )
    return {
        "added": normalized[: len(added)],
        "removed": normalized[len(added) : cut],
        "changed": changed,
    }


# Captures: every listener opens before the request and drains after it.


class _Drainer:
    """Collects items from a background reader until the stream has been quiet for a while."""

    def __init__(self) -> None:
        self.items: list[Any] = []
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def add(self, item: Any) -> None:
        with self._lock:
            self.items.append(item)
            self._last = time.monotonic()

    def touch(self) -> None:
        """Restart the quiet period: a drain begins only after the request has returned."""
        with self._lock:
            self._last = time.monotonic()

    def settled(self, started: float) -> bool:
        now = time.monotonic()
        with self._lock:
            quiet = now - self._last >= CAPTURE_QUIET_SECONDS
        return quiet or now - started >= CAPTURE_CAP_SECONDS

    def snapshot(self) -> list[Any]:
        with self._lock:
            return list(self.items)


class PgNotifyCapture:
    """LISTEN on the run database before the request, so every notification is captured."""

    def __init__(self, run: RunDatabase, channels: Sequence[str]) -> None:
        self._run = run
        self._channels = list(channels)
        self._connection: psycopg.Connection[Any] | None = None
        self._drainer = _Drainer()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> PgNotifyCapture:
        self._connection = psycopg.connect(self._run.url, autocommit=True)
        for channel in self._channels:
            self._connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return self

    def _read(self) -> None:
        assert self._connection is not None
        while not self._stop.is_set():
            for notice in self._connection.notifies(timeout=0.1, stop_after=1):
                self._drainer.add({"channel": notice.channel, "payload": notice.payload})

    def drain(self) -> list[dict[str, Any]]:
        self._drainer.touch()
        started = time.monotonic()
        while not self._drainer.settled(started):
            time.sleep(0.05)
        return list(self._drainer.snapshot())

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._connection is not None:
            self._connection.close()


@contextmanager
def pg_notify_capture(run: RunDatabase, channels: Sequence[str]) -> Iterator[PgNotifyCapture]:
    capture = PgNotifyCapture(run, channels)
    with capture:
        yield capture


def _decode_frame(frame: Any) -> Any:
    """Decode one text frame as JSON; a frame that is not JSON decodes to None and is skipped."""
    try:
        return json.loads(frame)
    except ValueError:
        return None


class WsPublishCapture:
    """A WS client connected before the request; keeps the frames whose type is declared."""

    def __init__(self, url: str, types: Sequence[str], *, headers: Mapping[str, str] | None = None):
        self._url = url
        self._types = frozenset(types)
        self._headers = dict(headers or {})
        self._drainer = _Drainer()
        self._socket: ClientConnection | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> WsPublishCapture:
        self._socket = connect(self._url, additional_headers=self._headers)
        try:
            self._subscribe()
        except BaseException:
            # __exit__ does not run when __enter__ raises, so the socket is closed here.
            self._socket.close()
            raise
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return self

    def _subscribe(self) -> None:
        """Subscribe to the declared types, then wait for the daemon's acknowledgement.

        The daemon delivers an event type only to a connection that subscribed to it (the
        broadcast filter), so the capture must subscribe before the request. Frames that arrive
        before the acknowledgement are kept when their type is declared.
        """
        assert self._socket is not None
        self._socket.send(json.dumps({"type": "subscribe", "events": sorted(self._types)}))
        deadline = time.monotonic() + CAPTURE_CAP_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecorderError("the daemon did not acknowledge the ws_publish subscription")
            try:
                frame = self._socket.recv(timeout=remaining)
            except TimeoutError as error:
                raise RecorderError(
                    "the daemon did not acknowledge the ws_publish subscription"
                ) from error
            decoded = _decode_frame(frame)
            if not isinstance(decoded, dict):
                continue
            if decoded.get("type") == "subscribe_success":
                return
            if decoded.get("type") == "error":
                raise RecorderError(f"the daemon rejected the ws_publish subscription: {decoded}")
            self._keep(decoded)

    def _keep(self, decoded: dict[str, Any]) -> None:
        if decoded.get("type") in self._types:
            self._drainer.add(decoded)

    def _read(self) -> None:
        assert self._socket is not None
        try:
            while True:
                decoded = _decode_frame(self._socket.recv())
                if isinstance(decoded, dict):
                    self._keep(decoded)
        except ConnectionClosed:
            return

    def drain(self) -> list[dict[str, Any]]:
        self._drainer.touch()
        started = time.monotonic()
        while not self._drainer.settled(started):
            time.sleep(0.05)
        return list(self._drainer.snapshot())

    def __exit__(self, *exc: object) -> None:
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@contextmanager
def ws_publish_capture(
    url: str, types: Sequence[str], *, headers: Mapping[str, str] | None = None
) -> Iterator[WsPublishCapture]:
    capture = WsPublishCapture(url, types, headers=headers)
    with capture:
        yield capture


def snapshot_files(root: Path, glob: str) -> dict[str, str]:
    """Map each regular file matching the glob (relative, posix) to its sha256; mtime is unused."""
    found: dict[str, str] = {}
    for path in sorted(root.glob(glob)):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def files_delta(before: Mapping[str, str], after: Mapping[str, str]) -> dict[str, list[str]]:
    """Return the created, modified and removed paths between two snapshots, sorted."""
    return {
        "created": sorted(set(after) - set(before)),
        "modified": sorted(
            path for path in set(before) & set(after) if before[path] != after[path]
        ),
        "removed": sorted(set(before) - set(after)),
    }


def files_captures(delta: Mapping[str, list[str]]) -> list[dict[str, str]]:
    """One capture per changed path, in change order then path order."""
    return [
        {"change": change, "path": path}
        for change in ("created", "modified", "removed")
        for path in delta[change]
    ]


# Senders: one per kind. Each returns the raw answer for normalization.


MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SESSION_HEADER = "mcp-session-id"
MCP_PROTOCOL_HEADER = "mcp-protocol-version"
MCP_REPLY_TIMEOUT_SECONDS = 60.0
MCP_INITIALIZE_ID = 1
MCP_CALL_ID = 2


def _initialize_message() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": MCP_INITIALIZE_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "gobby-contract-recorder", "version": "1"},
        },
    }


def _initialized_message() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _call_message(invocation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": MCP_CALL_ID,
        "method": "tools/call",
        "params": {"name": invocation["tool"], "arguments": invocation["arguments"]},
    }


def send_mcp_http(
    client: httpx.Client, invocation: Mapping[str, Any], *, operator: Mapping[str, str]
) -> dict[str, Any]:
    """Run one JSON-RPC tools/call over the streamable HTTP endpoint at /mcp.

    The session is initialized first, then the call is sent with the session and protocol
    headers; the reply is the JSON-RPC message for the call, whatever its result or error.
    """
    headers = {**operator, "Accept": "application/json, text/event-stream"}
    initialized = client.post("/mcp", headers=headers, json=_initialize_message())
    if initialized.status_code != 200:
        raise RecorderError(f"MCP initialize returned {initialized.status_code}")
    session = initialized.headers.get(MCP_SESSION_HEADER)
    if session is None:
        raise RecorderError("MCP initialize returned no session id")
    protocol = _rpc_message(initialized, MCP_INITIALIZE_ID)["result"]["protocolVersion"]
    session_headers = {**headers, MCP_SESSION_HEADER: session, MCP_PROTOCOL_HEADER: protocol}
    notified = client.post("/mcp", headers=session_headers, json=_initialized_message())
    if notified.status_code not in (200, 202):
        raise RecorderError(f"MCP initialized notification returned {notified.status_code}")
    call = client.post("/mcp", headers=session_headers, json=_call_message(invocation))
    return _rpc_message(call, MCP_CALL_ID)


def _rpc_message(response: httpx.Response, rpc_id: int) -> dict[str, Any]:
    """The JSON-RPC message with ``rpc_id`` from a JSON or server-sent-events reply."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        messages = [
            json.loads(line[len("data:") :].strip())
            for line in response.text.splitlines()
            if line.startswith("data:") and line[len("data:") :].strip()
        ]
    else:
        messages = [response.json()]
    for message in messages:
        if message.get("id") == rpc_id:
            return dict(message)
    raise RecorderError(f"no MCP reply with id {rpc_id} in a {response.status_code} response")


def _seal_home(home: Path) -> None:
    """Write the home's bootstrap.yaml at mode 0600 when the home has none.

    The file names two loopback ports that were free when it was written and lie outside the
    operator's range, a files home under the home and no database URL, so a bootstrap reader
    that ignores GOBBY_DAEMON_URL does not dial the operator's default port. A home that already
    has a bootstrap.yaml keeps it: the per-run daemon's home holds that daemon's own file.
    """
    path = home / "bootstrap.yaml"
    if path.exists():
        return
    files_home = home / "files"
    files_home.mkdir(parents=True, exist_ok=True)
    text = (
        "datastore_mode: local\n"
        "bind_host: 127.0.0.1\n"
        f"daemon_port: {e2e.find_free_port()}\n"
        f"websocket_port: {e2e.find_free_port()}\n"
        f"files_home: {files_home}\n"
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    path.chmod(0o600)


def _stdio_env(home: Path, daemon_url: str) -> dict[str, str]:
    """The stdio wrapper's environment: its isolated home, the test fence and the per-run daemon.

    The home is sealed first (see _seal_home). GOBBY_DAEMON_URL points the wrapper at the per-run
    daemon. Without it the wrapper's dial comes from the home's bootstrap.yaml, which names
    ports outside the operator's range. GOBBY_TEST_PROTECT with GOBBY_HOME limits its
    daemon scan to the processes whose command line names this home, so no other daemon is found
    or started.
    """
    _seal_home(home)
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GOBBY_HOME": str(home),
        "GOBBY_TEST_PROTECT": "1",
        "GOBBY_DAEMON_URL": daemon_url,
        "PYTHONUNBUFFERED": "1",
    }


def send_mcp_stdio(
    invocation: Mapping[str, Any], *, home: Path, cwd: Path, scratch: Path, daemon_url: str
) -> dict[str, Any]:
    """Run one JSON-RPC tools/call through the stdio MCP wrapper, as its clients spawn it.

    The wrapper's environment comes from ``_stdio_env``: the per-run home and the per-run daemon
    URL only. Its stderr goes to a scratch file for diagnosis.
    """
    env = _stdio_env(home, daemon_url)
    with (scratch / "mcp-stdio-stderr.log").open("w") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "gobby.mcp_proxy.stdio"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        )
        lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=_pump_lines, args=(process.stdout, lines), daemon=True).start()
        try:
            for message in (
                _initialize_message(),
                _initialized_message(),
                _call_message(invocation),
            ):
                assert process.stdin is not None
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
            replies: dict[int, dict[str, Any]] = {}
            while MCP_CALL_ID not in replies:
                replies.update(_stdio_reply(lines))
            return dict(replies[MCP_CALL_ID])
        finally:
            process.kill()
            process.wait()


def _pump_lines(stream: Any, lines: queue.Queue[str | None]) -> None:
    for line in stream:
        lines.put(line)
    lines.put(None)


def _stdio_reply(lines: queue.Queue[str | None]) -> dict[int, dict[str, Any]]:
    """The next JSON reply line from the wrapper, keyed by its id; logs on stdout are skipped."""
    while True:
        try:
            line = lines.get(timeout=MCP_REPLY_TIMEOUT_SECONDS)
        except queue.Empty as error:
            raise RecorderError("the stdio MCP wrapper did not reply in time") from error
        if line is None:
            raise RecorderError("the stdio MCP wrapper closed its stdout before replying")
        if line.lstrip().startswith("{"):
            message = json.loads(line)
            if "id" in message:
                return {int(message["id"]): message}


def send_ws(ws_url: str, frames: Sequence[str], *, operator: Mapping[str, str]) -> list[str]:
    """Send the frames on one connection and collect the text replies until the stream is quiet."""
    replies: list[str] = []
    with connect(ws_url, additional_headers=dict(operator)) as socket_:
        for frame in frames:
            socket_.send(frame)
        started = time.monotonic()
        while time.monotonic() - started < CAPTURE_CAP_SECONDS:
            try:
                replies.append(str(socket_.recv(timeout=CAPTURE_QUIET_SECONDS)))
            except TimeoutError:
                break
    return replies


def _cli_env(home: Path, daemon_url: str) -> dict[str, str]:
    """The CLI's sealed environment: a temporary home, the test fence and the per-run daemon URL.

    The home is sealed first (see _seal_home). GOBBY_DAEMON_URL sends resolver-based daemon
    calls to the per-run daemon. A caller that reads its port from bootstrap.yaml directly
    gets a port outside the operator's range, never the default port.
    """
    _seal_home(home)
    return {
        "PATH": f"{REPO_ROOT / '.venv' / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "GOBBY_HOME": str(home),
        "GOBBY_TEST_PROTECT": "1",
        "GOBBY_DAEMON_URL": daemon_url,
        "LANG": "C.UTF-8",
        "COLUMNS": "80",
    }


def send_cli(
    argv: Sequence[str], *, stdin: str | None, scratch: Path, daemon_url: str
) -> dict[str, Any]:
    """Run the checkout's gobby entry point in a sealed environment with a temporary home."""
    home = scratch / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = _cli_env(home, daemon_url)
    result = subprocess.run(
        [str(REPO_ROOT / ".venv" / "bin" / "gobby"), *argv],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(scratch),
    )
    return {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


# Observation: one case's normalized response and observed values, in declaration order.


@dataclass
class Observation:
    response: Any
    db_delta: list[Any] = field(default_factory=list)
    side_effects: list[Any] = field(default_factory=list)


def _normalize_response(case: Mapping[str, Any], raw: Any, window: Window) -> Any:
    """Redact the whole response, apply the case's pointer normalizers, then its masks.

    Redaction covers every kind: an HTTP body, a WS frame list and a CLI object alike. A WS
    frame is text, so its JSON is parsed, normalized and masked as a document and re-encoded
    canonically (sorted keys, compact separators); pointers inside a frame therefore address its
    fields. Masks are read under /response, as the comparison reads them. An HTTP answer is
    masked once its unordered collections are sorted and before its field rules run, so a
    positional mask addresses the sorted list.
    """
    rules = case.get("normalizers", {})
    mask = case.get("mask", [])
    if corpus_v3.case_kind(case) == "ws":
        frames = [_parse_frame(frame) for frame in raw]
        response: Any = http_corpus.redact_secrets(frames)
        if rules:
            response = normalize_document({"response": response}, rules, window=window)["response"]
        masked = http_corpus.apply_masks({"response": response}, mask)["response"]
        return [_canonical_frame(frame) for frame in masked]
    response = http_corpus.redact_secrets(json_form(raw))
    if corpus_v3.case_kind(case) == "http":
        # As the Rust comparison does: unordered collections sort first, the masks then apply to
        # the sorted answer, and the field rules observe the masked answer.
        collections = {
            pointer: spec for pointer, spec in rules.items() if unordered_key(spec) is not None
        }
        if collections:
            response = normalize_document({"response": response}, collections, window=window)[
                "response"
            ]
        response = http_corpus.apply_masks({"response": response}, mask)["response"]
        rules = {pointer: spec for pointer, spec in rules.items() if pointer not in collections}
    if rules:
        response = normalize_document({"response": response}, rules, window=window)["response"]
    return http_corpus.apply_masks({"response": response}, mask)["response"]


def _parse_frame(frame: Any) -> Any:
    try:
        return json.loads(frame)
    except (TypeError, ValueError) as error:
        raise RecorderError(f"a WS reply is not JSON: {frame!r}") from error


def _canonical_frame(frame: Any) -> str:
    return json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _run_case(
    run: RunDatabase,
    daemon: Any,
    case: Mapping[str, Any],
    *,
    operator: Mapping[str, str],
    scratch: Path,
    edge: ProviderEdgeLog | None = None,
) -> tuple[Observation, Window]:
    """Run one case end to end and return its raw-but-normalized observation and its window."""
    kind = corpus_v3.case_kind(case)
    seed = case.get("seed", [])
    with httpx.Client(base_url=daemon.http_url, timeout=30.0) as client:
        apply_seed(
            run,
            seed,
            send_http=lambda step: send_seed_http(client, step, operator=operator),
        )
        entries = list(case.get("db_delta", []))
        effects = list(case.get("side_effects", []))
        before = [snapshot_table(run, entry) for entry in entries]
        files_before = {
            index: snapshot_files(daemon.home / "files", effect["match"]["glob"])
            for index, effect in enumerate(effects)
            if effect["channel"] == "files_home_write"
        }
        with ExitStack() as stack:
            captures: dict[int, Any] = {}
            for index, effect in enumerate(effects):
                channel = effect["channel"]
                if channel == "pg_notify":
                    captures[index] = stack.enter_context(
                        pg_notify_capture(run, effect["match"]["channels"])
                    )
                elif channel == "ws_publish":
                    captures[index] = stack.enter_context(
                        ws_publish_capture(
                            f"ws://127.0.0.1:{daemon.ws_port}/",
                            effect["match"]["types"],
                            headers=operator,
                        )
                    )
                elif channel == "provider_edge":
                    if edge is None:
                        raise RecorderError("a provider_edge side effect needs an edge")
                    captures[index] = stack.enter_context(
                        ProviderEdgeCapture(edge, effect["match"]["hosts"])
                    )
            started = datetime.now(UTC)
            response = _send(kind, case, client, daemon, operator=operator, scratch=scratch)
            drained = {index: capture.drain() for index, capture in captures.items()}
        finished = datetime.now(UTC)
        window = Window(start=started, end=finished)
        after = [snapshot_table(run, entry) for entry in entries]
    observed_db = [
        table_delta(b, a, e, window=window) for b, a, e in zip(before, after, entries, strict=True)
    ]
    observed_effects: list[Any] = []
    for index, effect in enumerate(effects):
        if effect["channel"] == "files_home_write":
            delta = files_delta(
                files_before[index], snapshot_files(daemon.home / "files", effect["match"]["glob"])
            )
            items: list[Any] = files_captures(delta)
        else:
            items = drained[index]
        observed_effects.append(
            normalize_captures(items, effect.get("normalizers", {}), window=window)
        )
    normalized = _normalize_response(case, response, window)
    return Observation(
        response=normalized, db_delta=observed_db, side_effects=observed_effects
    ), window


def _send(
    kind: str,
    case: Mapping[str, Any],
    client: httpx.Client,
    daemon: Any,
    *,
    operator: Mapping[str, str],
    scratch: Path,
) -> Any:
    """Send one case of the given kind and return its raw response, redacted by kind."""
    if kind == "http":
        headers = recorder_credential_headers(client, case["credential"], operator)
        request = case["request"]
        headers.update(request["headers"])
        response = client.request(
            request["method"],
            request["path"],
            params=request["query"],
            headers=headers,
            json=request["body"],
        )
        # Redacted with allowlisted headers but not masked: _normalize_response masks the answer
        # after it sorts the unordered collections.
        return http_corpus.normalize_response({**case, "mask": []}, response)
    if kind == "mcp":
        invocation = case["invocation"]
        if invocation["transport"] == "rest":
            return send_mcp_rest(client, invocation, operator=operator)
        if invocation["transport"] == "stdio":
            return send_mcp_stdio(
                invocation,
                home=daemon.home,
                cwd=daemon.project_dir,
                scratch=scratch,
                daemon_url=daemon.http_url,
            )
        return send_mcp_http(client, invocation, operator=operator)
    invocation = case["invocation"]
    if kind == "ws":
        return send_ws(f"ws://127.0.0.1:{daemon.ws_port}/", invocation["frames"], operator=operator)
    if kind == "cli":
        return send_cli(
            invocation["argv"],
            stdin=invocation.get("stdin"),
            scratch=scratch,
            daemon_url=daemon.http_url,
        )
    raise RecorderError(f"unknown kind {kind!r}")


# Replay and recording entries.


def _classify_inputs(observation: Observation) -> tuple[Any, dict[str, Any]]:
    observed = {"db_delta": observation.db_delta, "side_effects": observation.side_effects}
    return observation.response, observed


def replay_case(
    run: RunDatabase,
    daemon: Any,
    case: Mapping[str, Any],
    waivers: Mapping[str, Waiver],
    *,
    operator: Mapping[str, str],
    scratch: Path,
    edge: ProviderEdgeLog | None = None,
) -> http_corpus.ReplayResult:
    """Replay one stored schema 3 case and classify it against its recording or native answer."""
    observation, _window = _run_case(
        run, daemon, case, operator=operator, scratch=scratch, edge=edge
    )
    actual, observed = _classify_inputs(observation)
    return http_corpus.classify_replay(case, actual, waivers, observed=observed)


def _recorded_entries(case: dict[str, Any], observation: Observation) -> None:
    """Attach redacted, masked recorded values to the case's db_delta and side_effects, in place.

    Redaction runs after normalization, as the comparison does, and captures are redacted in full
    (every body and capture). The case's masks then apply at the pointers the comparison uses,
    which address each value under ``recorded``.
    """
    case_name = str(case.get("name", ""))
    recorded: dict[str, Any] = {
        "db_delta": [{"recorded": recorded_db_delta(v, case_name)} for v in observation.db_delta],
        "side_effects": [
            {"recorded": http_corpus.redact_secrets(v)} for v in observation.side_effects
        ],
    }
    masked = http_corpus.apply_masks(recorded, case.get("mask", []))
    for name in ("db_delta", "side_effects"):
        for entry, value in zip(case.get(name, []), masked[name], strict=True):
            entry["recorded"] = value["recorded"]


def record_template(
    run: RunDatabase,
    daemon: Any,
    template: Mapping[str, Any],
    *,
    operator: Mapping[str, str],
    scratch: Path,
    edge: ProviderEdgeLog | None = None,
) -> dict[str, Any]:
    """Run one raw template and return the recorded case, keys in the frozen order.

    A capture that holds a provider request still in flight is refused, never recorded.
    """
    case = copy.deepcopy(dict(template))
    observation, _window = _run_case(
        run, daemon, case, operator=operator, scratch=scratch, edge=edge
    )
    refuse_pending_captures(str(case.get("name", "")), observation.side_effects)
    if "waiver" in case and str(case["waiver"]).endswith(".native-extension"):
        case.pop("response", None)
    else:
        case["response"] = observation.response
    _recorded_entries(case, observation)
    return {key: case[key] for key in corpus_v3.V3_KEY_ORDER if key in case}


def case_bytes(case: Mapping[str, Any]) -> bytes:
    return (json.dumps(case, indent=2, ensure_ascii=False) + "\n").encode()


def read_template(path: Path) -> dict[str, Any]:
    """Read one unrecorded template raw: the loader rejects it, so no validation runs here."""
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise CorpusError(f"{path.name} must be a JSON object")
    return document


def startup_for(family: str, manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The manifest's startup block for one family, or None when the family has none."""
    startup: Mapping[str, Any] = manifest.get("startup", {})
    entry: Mapping[str, Any] | None = startup.get(family)
    return entry


def record_in_own_run(
    template: Mapping[str, Any],
    *,
    scratch: Path,
    startup: Mapping[str, Any] | None = None,
    edge: FakeEdge | None = None,
) -> dict[str, Any]:
    """Record one template in a run database and a daemon of its own.

    No case reads another case's writes, so every case starts from an empty run. The daemon
    starts from the case family's startup block, if it has one, and routes through the edge.
    """
    with (
        open_run_database() as run,
        recorder_daemon(
            base_url=run.base_url, database_url=run.url, startup=startup, edge=edge
        ) as daemon,
    ):
        return record_template(
            run,
            daemon,
            template,
            operator=daemon.operator_headers(),
            scratch=scratch,
            edge=edge,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Record every template in the templates directory into the corpus directory.

    One provider edge serves the whole run, built before the first daemon and stopped after the
    last. Its test authority lives in the run's scratch directory.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--templates", type=Path, default=TEMPLATE_DIR)
    parser.add_argument("--out", type=Path, default=V3_DIR)
    args = parser.parse_args(argv)
    templates = sorted(args.templates.glob("*.json"))
    scratch = Path(tempfile.mkdtemp(prefix="recorder-v3-"))
    manifest = corpus_v3.load_manifest(V3_DIR)
    edge = FakeEdge(scratch / "edge")
    (scratch / "edge").mkdir(parents=True, exist_ok=True)
    edge.start()
    try:
        for template in templates:
            document = read_template(template)
            startup = startup_for(str(document.get("family", "")), manifest)
            case = record_in_own_run(document, scratch=scratch, startup=startup, edge=edge)
            (args.out / f"{case['name']}.json").write_bytes(case_bytes(case))
            print(f"recorded {case['name']}", file=sys.stderr)
    finally:
        edge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
