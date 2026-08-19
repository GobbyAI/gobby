from __future__ import annotations

import asyncio
import multiprocessing as mp
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import psycopg
import pytest

from gobby.ai.embedding_switch import (
    advance_phase,
    build_physical_names,
    complete_switch,
    get_switch_status,
    managed_embedding_projection,
    persist_journal,
    start_switch,
)
from gobby.config.embedding_keys import AI_EMBEDDING_API_KEY_KEY
from gobby.config.runtime import ConfigRuntime
from gobby.config.runtime_models import ConfigChange, ConfigSnapshot
from gobby.storage.config_mutations import (
    ConfigConflictError,
    ConfigMutations,
    ConfigPatch,
    SecretUpdate,
)
from gobby.storage.config_notifications import ConfigNotificationListener
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.config_store import ConfigStore
from gobby.storage.embedding_generation_state import (
    EmbeddingGenerationLeaseLost,
    EmbeddingGenerationState,
    EmbeddingServingLease,
    ProjectionChange,
    managed_projection_targets,
)
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.secrets import POSTURE_SCRYPT_PASSPHRASE, SecretStore
from tests.integration.config.worker_entry import WorkerSpec, _worker_entry

LIVE_KEY = "rules.enforcement_enabled"
RESTART_KEY = "ui.enabled"
SECRET_KEY = AI_EMBEDDING_API_KEY_KEY
VISIBLE_KEYS = (LIVE_KEY, RESTART_KEY, SECRET_KEY, "ai.embeddings.model")
COMMAND_TIMEOUT = 15.0


class RemoteConfigConflict(RuntimeError):
    def __init__(self, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"Configuration revision conflict: expected {expected_revision}, "
            f"current revision is {actual_revision}"
        )


class RemoteWorkerError(RuntimeError):
    pass


class _Prepared:
    def __init__(self, value: object) -> None:
        self.value = value

    def dispose(self) -> None:
        return None


class _ProbeSubscriber:
    name = "multi-daemon-probe"
    keys = frozenset({LIVE_KEY, SECRET_KEY})
    required = True
    prepare_timeout = 1.0
    dispose_timeout = 1.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failed_keys: set[str] = set()
        self._revisions: list[int] = []

    def fail(self, keys: set[str]) -> None:
        with self._lock:
            self._failed_keys = set(keys)

    def prepare(self, change: ConfigChange) -> _Prepared:
        with self._lock:
            self._revisions.append(change.revision)
            failed = self._failed_keys.intersection(change.changed_keys)
        if failed:
            raise RuntimeError(f"probe rejected {sorted(failed)}")
        return _Prepared(change.revision)

    def state(self) -> dict[str, object]:
        with self._lock:
            return {"revisions": list(self._revisions)}


class _TrackingNotificationSource:
    def __init__(self, listener: ConfigNotificationListener) -> None:
        self._listener = listener
        self._processed: set[int] = set()
        self._events: dict[int, asyncio.Event] = {}

    async def connect(self) -> None:
        await self._listener.connect()

    async def revisions(self) -> AsyncIterator[int]:
        async for revision in self._listener.revisions():
            yield revision
            self._processed.add(revision)
            event = self._events.get(revision)
            if event is not None:
                event.set()

    async def wait_processed(self, revision: int) -> None:
        if revision in self._processed:
            return
        event = self._events.setdefault(revision, asyncio.Event())
        await asyncio.wait_for(event.wait(), timeout=COMMAND_TIMEOUT)

    async def close(self) -> None:
        await self._listener.close()


@dataclass(slots=True)
class _WorkerState:
    db: PostgresHubDatabase
    secret_store: SecretStore
    store: ConfigStore
    mutations: ConfigMutations
    repository: ConfigRepository
    listener: _TrackingNotificationSource
    subscriber: _ProbeSubscriber
    runtime: ConfigRuntime
    listener_pid: int | None
    listener_allowed: threading.Event
    startup_error: str | None
    lease: EmbeddingServingLease | None = None
    lease_generation_state: EmbeddingGenerationState | None = None
    lease_activated_at: float | None = None


def _snapshot_payload(snapshot: ConfigSnapshot, subscriber: _ProbeSubscriber) -> dict[str, object]:
    desired = {key: snapshot.desired_values.get(key) for key in VISIBLE_KEYS}
    active = {key: snapshot.active_values.get(key) for key in VISIBLE_KEYS}
    return {
        "revision": snapshot.revision,
        "desired": desired,
        "active": active,
        "pending_restart_keys": sorted(snapshot.pending_restart_keys),
        "failed_live_keys": sorted(snapshot.failed_live_keys),
        "desired_secret_fingerprint": snapshot.desired_secret_fingerprint(SECRET_KEY),
        "active_secret_fingerprint": snapshot.active_secret_fingerprint(SECRET_KEY),
        "subscriber": subscriber.state(),
    }


async def _build_state(spec: WorkerSpec) -> _WorkerState:
    db = PostgresHubDatabase(spec.dsn)
    secret_store = SecretStore(db, gobby_home=spec.home, kek_passphrase=spec.passphrase)
    store = ConfigStore(db, secret_store=secret_store)
    mutations = ConfigMutations(db, secret_store=secret_store)
    repository = ConfigRepository(db, secret_store=secret_store)
    listener_pid: int | None = None
    listener_allowed = threading.Event()
    listener_allowed.set()

    async def open_listener() -> Any:
        nonlocal listener_pid
        if not listener_allowed.is_set():
            raise psycopg.OperationalError("simulated PostgreSQL partition")
        connection = await psycopg.AsyncConnection.connect(
            spec.dsn,
            autocommit=True,
            application_name=f"config-test-listener-{spec.name}",
        )
        listener_pid = connection.info.backend_pid
        return connection

    listener = _TrackingNotificationSource(ConfigNotificationListener(open_listener))
    subscriber = _ProbeSubscriber()
    runtime = ConfigRuntime(
        repository,
        subscribers=[subscriber],
        notification_source=listener,
        managed_resolver=managed_embedding_projection,
        reconnect_backoff=0.02,
    )
    startup_error: str | None = None
    try:
        await runtime.start()
    except Exception as exc:
        # Keep the process-boundary contract as ``TypeName: message``. The
        # wrong-KEK daemon test asserts the exception type survives serialization.
        startup_error = f"{type(exc).__name__}: {exc}"
    return _WorkerState(
        db=db,
        secret_store=secret_store,
        store=store,
        mutations=mutations,
        repository=repository,
        listener=listener,
        subscriber=subscriber,
        runtime=runtime,
        listener_pid=listener_pid,
        listener_allowed=listener_allowed,
        startup_error=startup_error,
    )


def _secret_updates(raw: object) -> dict[str, SecretUpdate]:
    updates = cast(dict[str, dict[str, object]], raw or {})
    return {
        key: SecretUpdate(
            plaintext=cast(str, value["plaintext"]),
            name=cast(str | None, value.get("name")),
        )
        for key, value in updates.items()
    }


def _change_payload(change: ProjectionChange) -> dict[str, object]:
    return {
        "sequence": change.sequence,
        "source_kind": change.source_kind,
        "source_id": change.source_id,
        "is_tombstone": change.is_tombstone,
    }


async def _dispatch(state: _WorkerState, request: dict[str, object]) -> object:
    operation = cast(str, request["operation"])
    if operation == "status":
        return {
            "ready": state.runtime.ready,
            "healthy": state.runtime.healthy,
            "startup_error": state.startup_error,
        }
    if operation == "snapshot":
        return _snapshot_payload(state.runtime.snapshot, state.subscriber)
    if operation == "secret_state":
        snapshot = state.runtime.snapshot
        return {
            "desired": snapshot.desired_secret(SECRET_KEY),
            "active": snapshot.active_secret(SECRET_KEY),
            "desired_fingerprint": snapshot.desired_secret_fingerprint(SECRET_KEY),
            "active_fingerprint": snapshot.active_secret_fingerprint(SECRET_KEY),
        }
    if operation == "set_fail":
        state.subscriber.fail(set(cast(list[str], request["keys"])))
        return True
    if operation == "patch":
        expected_revision = cast(int, request["expected_revision"])
        patch = ConfigPatch(
            values=cast(dict[str, object], request.get("values", {})),
            unset=frozenset(cast(list[str], request.get("unset", []))),
            secrets=_secret_updates(request.get("secrets")),
        )
        result = await asyncio.to_thread(
            state.mutations.patch,
            expected_revision=expected_revision,
            patch=patch,
        )
        if cast(bool, request.get("reconcile", True)):
            await state.runtime.reconcile_local_commit(result.revision)
        return result.revision
    if operation == "listener_pid":
        return state.listener_pid
    if operation == "wait_notification":
        await state.listener.wait_processed(cast(int, request["revision"]))
        return True
    if operation == "switch_start":
        journal, _ = await asyncio.to_thread(
            start_switch,
            state.store,
            "qwen3-8b-q8",
            "ollama",
            current_dim=768,
            current_catalog_id="nomic-v1.5-f16",
        )
        journal.physical_names = build_physical_names(journal)
        await asyncio.to_thread(persist_journal, state.store, journal)
        return journal.to_dict()
    if operation == "switch_status":
        status_journal = await asyncio.to_thread(get_switch_status, state.store)
        return None if status_journal is None else status_journal.to_dict()
    if operation == "switch_phase":
        phase_journal = await asyncio.to_thread(get_switch_status, state.store)
        if phase_journal is None:
            raise RuntimeError("embedding switch journal is missing")
        updated = await asyncio.to_thread(
            advance_phase,
            state.store,
            phase_journal,
            cast(str, request["phase"]),
        )
        return updated.to_dict()
    if operation == "switch_complete":
        complete_journal = await asyncio.to_thread(get_switch_status, state.store)
        if complete_journal is None:
            raise RuntimeError("embedding switch journal is missing")
        record = await asyncio.to_thread(complete_switch, state.store, complete_journal)
        if cast(bool, request.get("reconcile", True)):
            await state.runtime.reconcile_local_commit(record.committed_revision)
        return record.to_dict()
    if operation == "append_change":
        generation = EmbeddingGenerationState(state.db)
        return await asyncio.to_thread(
            generation.append_change,
            cast(str, request["source_kind"]),
            cast(str, request["source_id"]),
            is_tombstone=cast(bool, request.get("is_tombstone", False)),
        )
    if operation == "changes_after":
        changes = await asyncio.to_thread(
            EmbeddingGenerationState(state.db).changes_after,
            cast(int, request["sequence"]),
        )
        return [_change_payload(change) for change in changes]
    if operation == "projection_targets":
        return await asyncio.to_thread(
            EmbeddingGenerationState(state.db).projection_targets,
            cast(str, request["source_kind"]),
            cast(str, request["primary"]),
        )
    if operation == "managed_targets":
        return list(
            managed_projection_targets(
                state.runtime.capture().managed,
                cast(str, request["source_kind"]),
                cast(str, request["primary"]),
            )
        )
    if operation == "current_revision":
        return await asyncio.to_thread(state.repository.current_revision)
    if operation == "lease_activate":
        generation = EmbeddingGenerationState(state.db)
        watermark = await asyncio.to_thread(generation.watermark)
        caught_up_watermark = cast(int | None, request.get("caught_up_watermark"))
        if caught_up_watermark is None:
            caught_up_watermark = watermark
        state.lease = generation.prepare_serving_lease(
            UUID(cast(str, request["daemon_id"])),
            cast(str, request["generation"]),
            cast(int, request["revision"]),
            lease_seconds=cast(float, request["lease_seconds"]),
            caught_up_watermark=caught_up_watermark,
            required_watermark=watermark,
        )
        state.lease_generation_state = generation
        state.lease_activated_at = time.monotonic()
        await asyncio.to_thread(state.lease.activate)
        return True
    if operation == "lease_wait_fenced":
        if state.lease is None:
            raise RuntimeError("serving lease is missing")
        deadline = time.monotonic() + cast(float, request["timeout"])
        while True:
            try:
                state.lease.assert_serving()
            except EmbeddingGenerationLeaseLost as exc:
                activated_at = state.lease_activated_at
                if activated_at is None:
                    raise RuntimeError("serving lease was never activated") from exc
                return {
                    "fenced_after": time.monotonic() - activated_at,
                    "message": str(exc),
                }
            if time.monotonic() >= deadline:
                raise RuntimeError("partitioned lease never self-fenced")
            await asyncio.sleep(0.02)
    if operation == "watermark":
        return await asyncio.to_thread(EmbeddingGenerationState(state.db).watermark)
    if operation == "lease_renew":
        if state.lease is None:
            raise RuntimeError("serving lease is missing")
        await asyncio.to_thread(state.lease.renew)
        return True
    if operation == "lease_assert":
        if state.lease is None:
            raise RuntimeError("serving lease is missing")
        await asyncio.to_thread(state.lease.assert_serving)
        return True
    if operation == "can_collect":
        return await asyncio.to_thread(
            EmbeddingGenerationState(state.db).can_collect,
            cast(str, request["generation"]),
            cast(int, request["revision"]),
        )
    if operation == "wait_collectible":
        generation_state = EmbeddingGenerationState(state.db)
        deadline = time.monotonic() + cast(float, request["timeout"])
        while time.monotonic() < deadline:
            collectible = await asyncio.to_thread(
                generation_state.can_collect,
                cast(str, request["generation"]),
                cast(int, request["revision"]),
            )
            if collectible:
                return True
            await asyncio.sleep(0.02)
        return False
    if operation == "partition":
        state.listener_allowed.clear()
        await state.listener.close()
        await asyncio.to_thread(state.db.close)
        return True
    if operation == "heal":
        # Connectivity returns: allow the listener factory again and swap a
        # fresh pool into every held storage object in place (a closed
        # PostgresHubDatabase pool cannot reopen). The runtime and lease keep
        # their existing object references, so this is an in-process heal —
        # no restart, no rebuilt runtime.
        state.listener_allowed.set()
        healed_db = PostgresHubDatabase(state.db.conninfo)
        state.secret_store.db = healed_db
        state.store.db = healed_db
        state.store.repository.db = healed_db
        state.mutations.db = healed_db
        state.repository.db = healed_db
        if state.lease_generation_state is not None:
            state.lease_generation_state.db = healed_db
        state.db = healed_db
        return True
    raise ValueError(f"unknown worker operation: {operation}")


async def _serve(connection: Connection, spec: WorkerSpec) -> None:
    state = await _build_state(spec)
    connection.send(
        {
            "ok": True,
            "result": {
                "ready": state.runtime.ready,
                "healthy": state.runtime.healthy,
                "startup_error": state.startup_error,
            },
        }
    )
    try:
        while True:
            try:
                request = await asyncio.to_thread(connection.recv)
            except EOFError:
                break
            if request["operation"] == "close":
                connection.send({"ok": True, "result": True})
                break
            try:
                result = await _dispatch(state, request)
            except ConfigConflictError as exc:
                connection.send(
                    {
                        "ok": False,
                        "kind": "conflict",
                        "expected_revision": exc.expected_revision,
                        "actual_revision": exc.actual_revision,
                    }
                )
            except Exception as exc:
                connection.send(
                    {
                        "ok": False,
                        "kind": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                connection.send({"ok": True, "result": result})
    finally:
        await state.runtime.close()
        await asyncio.to_thread(state.db.close)
        connection.close()


class DaemonWorker:
    def __init__(self, spec: WorkerSpec, *, allow_start_failure: bool = False) -> None:
        self.spec = spec
        self._allow_start_failure = allow_start_failure
        self._lock = threading.Lock()
        self._process: Any = None
        self._connection: Connection | None = None
        self.start()

    def start(self) -> None:
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_worker_entry,
            args=(child, self.spec),
            name=f"config-runtime-{self.spec.name}",
        )
        process.start()
        child.close()
        self._process = process
        self._connection = parent
        startup = self._receive()
        if not self._allow_start_failure and not cast(bool, startup["ready"]):
            error = cast(str, startup["startup_error"])
            self.stop()
            raise RemoteWorkerError(error)

    def _receive(self) -> dict[str, object]:
        connection = self._connection
        if connection is None or not connection.poll(COMMAND_TIMEOUT):
            raise TimeoutError(f"worker {self.spec.name} did not respond")
        response = cast(dict[str, object], connection.recv())
        if cast(bool, response["ok"]):
            return cast(dict[str, object], response["result"])
        if response["kind"] == "conflict":
            raise RemoteConfigConflict(
                cast(int, response["expected_revision"]),
                cast(int, response["actual_revision"]),
            )
        raise RemoteWorkerError(cast(str, response["error"]))

    def _request_value(self, operation: str, **payload: object) -> object:
        connection = self._connection
        if connection is None:
            raise RuntimeError(f"worker {self.spec.name} is stopped")
        with self._lock:
            connection.send({"operation": operation, **payload})
            if not connection.poll(COMMAND_TIMEOUT):
                raise TimeoutError(f"worker {self.spec.name} did not respond")
            response = cast(dict[str, object], connection.recv())
        if cast(bool, response["ok"]):
            return response.get("result")
        if response["kind"] == "conflict":
            raise RemoteConfigConflict(
                cast(int, response["expected_revision"]),
                cast(int, response["actual_revision"]),
            )
        raise RemoteWorkerError(cast(str, response["error"]))

    def status(self) -> dict[str, object]:
        return cast(dict[str, object], self._request_value("status"))

    def snapshot(self) -> dict[str, object]:
        return cast(dict[str, object], self._request_value("snapshot"))

    def wait_for_revision(self, revision: int, *, timeout: float = 5.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            if cast(int, snapshot["revision"]) >= revision:
                return snapshot
            time.sleep(0.02)
        raise TimeoutError(f"worker {self.spec.name} did not reach revision {revision}")

    def patch(
        self,
        *,
        expected_revision: int,
        values: dict[str, object] | None = None,
        unset: list[str] | None = None,
        secrets: dict[str, dict[str, object]] | None = None,
        reconcile: bool = True,
    ) -> int:
        return cast(
            int,
            self._request_value(
                "patch",
                expected_revision=expected_revision,
                values=values or {},
                unset=unset or [],
                secrets=secrets or {},
                reconcile=reconcile,
            ),
        )

    def set_fail(self, *keys: str) -> None:
        self._request_value("set_fail", keys=list(keys))

    def secret_state(self) -> dict[str, object]:
        return cast(dict[str, object], self._request_value("secret_state"))

    def wait_for_notification(self, revision: int) -> None:
        self._request_value("wait_notification", revision=revision)

    def switch_start(self) -> dict[str, object]:
        return cast(dict[str, object], self._request_value("switch_start"))

    def switch_status(self) -> dict[str, object] | None:
        return cast(dict[str, object] | None, self._request_value("switch_status"))

    def managed_targets(self, source_kind: str, primary: str) -> list[str]:
        return cast(
            list[str],
            self._request_value("managed_targets", source_kind=source_kind, primary=primary),
        )

    def current_revision(self) -> int:
        return cast(int, self._request_value("current_revision"))

    def switch_phase(self, phase: str) -> dict[str, object]:
        return cast(
            dict[str, object],
            self._request_value("switch_phase", phase=phase),
        )

    def switch_complete(self, *, reconcile: bool = True) -> dict[str, object]:
        return cast(
            dict[str, object],
            self._request_value("switch_complete", reconcile=reconcile),
        )

    def append_change(
        self,
        source_kind: str,
        source_id: str,
        *,
        is_tombstone: bool = False,
    ) -> int:
        return cast(
            int,
            self._request_value(
                "append_change",
                source_kind=source_kind,
                source_id=source_id,
                is_tombstone=is_tombstone,
            ),
        )

    def changes_after(self, sequence: int) -> list[dict[str, object]]:
        return cast(
            list[dict[str, object]],
            self._request_value("changes_after", sequence=sequence),
        )

    def projection_targets(self, source_kind: str, primary: str) -> tuple[str, ...]:
        return cast(
            tuple[str, ...],
            self._request_value(
                "projection_targets",
                source_kind=source_kind,
                primary=primary,
            ),
        )

    def lease_activate(
        self,
        daemon_id: UUID,
        generation: str,
        revision: int,
        *,
        lease_seconds: float,
        caught_up_watermark: int | None = None,
    ) -> None:
        self._request_value(
            "lease_activate",
            daemon_id=str(daemon_id),
            generation=generation,
            revision=revision,
            lease_seconds=lease_seconds,
            caught_up_watermark=caught_up_watermark,
        )

    def lease_renew(self) -> None:
        self._request_value("lease_renew")

    def lease_assert(self) -> None:
        self._request_value("lease_assert")

    def lease_wait_fenced(self, *, timeout: float) -> dict[str, object]:
        """Block in-process until the lease self-fences; report seconds since activation."""
        return cast(
            dict[str, object],
            self._request_value("lease_wait_fenced", timeout=timeout),
        )

    def watermark(self) -> int:
        return cast(int, self._request_value("watermark"))

    def heal(self) -> None:
        self._request_value("heal")

    def can_collect(self, generation: str, revision: int) -> bool:
        return cast(
            bool,
            self._request_value(
                "can_collect",
                generation=generation,
                revision=revision,
            ),
        )

    def wait_until_collectible(
        self,
        generation: str,
        revision: int,
        *,
        timeout: float,
    ) -> bool:
        return cast(
            bool,
            self._request_value(
                "wait_collectible",
                generation=generation,
                revision=revision,
                timeout=timeout,
            ),
        )

    def partition(self) -> None:
        self._request_value("partition")

    def stop(self) -> None:
        connection = self._connection
        process = self._process
        if connection is None or process is None:
            return
        if process.is_alive():
            try:
                self._request_value("close")
            finally:
                process.join(COMMAND_TIMEOUT)
        connection.close()
        self._connection = None
        if process.is_alive():
            process.terminate()
            process.join(COMMAND_TIMEOUT)

    def crash(self) -> None:
        process = self._process
        if process is not None and process.is_alive():
            process.terminate()
            process.join(COMMAND_TIMEOUT)
        if self._connection is not None:
            self._connection.close()
        self._connection = None

    def restart(self) -> None:
        self.crash()
        self.start()


class TwoDaemonCluster:
    def __init__(self, dsn: str, root: Path, passphrase: str) -> None:
        self.dsn = dsn
        self.root = root
        self.passphrase = passphrase
        self.first = DaemonWorker(WorkerSpec("first", dsn, root / "first", passphrase))
        self.second = DaemonWorker(WorkerSpec("second", dsn, root / "second", passphrase))
        self._extras: list[DaemonWorker] = []

    def terminate_listener(self, worker: DaemonWorker) -> int:
        pid = cast(int, worker._request_value("listener_pid"))
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            row = connection.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()
        if row is None or not bool(row[0]):
            raise RuntimeError(f"listener backend {pid} was not terminated")
        return pid

    def add_worker(self, name: str, passphrase: str) -> DaemonWorker:
        worker = DaemonWorker(
            WorkerSpec(name, self.dsn, self.root / name, passphrase),
            allow_start_failure=True,
        )
        self._extras.append(worker)
        return worker

    def close(self) -> None:
        for worker in [*self._extras, self.second, self.first]:
            worker.stop()


@pytest.fixture
def two_daemons(
    postgres_db: PostgresHubDatabase,
    tmp_path: Path,
) -> Iterator[TwoDaemonCluster]:
    passphrase = "shared-test-passphrase"
    secret_store = SecretStore(
        postgres_db,
        gobby_home=tmp_path / "seed",
        kek_passphrase=passphrase,
    )
    secret_store.set_kek_posture(POSTURE_SCRYPT_PASSPHRASE, passphrase=passphrase)
    ConfigRepository(postgres_db, secret_store=secret_store).reconcile_registry()
    cluster = TwoDaemonCluster(postgres_db.conninfo, tmp_path, passphrase)
    try:
        yield cluster
    finally:
        cluster.close()


__all__ = [
    "DaemonWorker",
    "LIVE_KEY",
    "RESTART_KEY",
    "RemoteConfigConflict",
    "RemoteWorkerError",
    "SECRET_KEY",
    "TwoDaemonCluster",
]
