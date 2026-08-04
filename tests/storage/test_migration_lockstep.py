from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Barrier, Event, Lock
from types import MethodType
from typing import Any, cast

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.migrations import Migration, MigrationRunner, MigrationUnsupportedError


class _Result:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


@dataclass
class _SchemaState:
    applied: set[int]
    advisory_lock: Lock = field(default_factory=Lock)
    writes: list[str] = field(default_factory=list)
    attempts: dict[str, Event] = field(default_factory=lambda: {"new": Event(), "old": Event()})

    @property
    def head(self) -> int | None:
        return max(self.applied) if self.applied else None


class _Connection:
    def __init__(self, state: _SchemaState, actor: str, *, lock_connection: bool) -> None:
        self._state = state
        self._actor = actor
        self._lock_connection = lock_connection
        self._owns_lock = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Result:
        normalized = " ".join(sql.split())
        if "pg_advisory_lock" in normalized:
            assert self._lock_connection
            self._state.attempts[self._actor].set()
            self._state.advisory_lock.acquire()
            self._owns_lock = True
            return _Result()
        if "pg_advisory_unlock" in normalized:
            assert self._owns_lock
            self._state.advisory_lock.release()
            self._owns_lock = False
            return _Result([{"pg_advisory_unlock": True}])
        if normalized == "SELECT MAX(version) AS version FROM schema_migrations":
            return _Result([{"version": self._state.head}])
        if normalized == "SELECT version FROM schema_migrations":
            return _Result([{"version": version} for version in sorted(self._state.applied)])
        if "SELECT version FROM schema_migrations WHERE version = %s" in normalized:
            version = int(params[0])
            rows = [{"version": version}] if version in self._state.applied else []
            return _Result(rows)
        if normalized.startswith("CREATE TABLE IF NOT EXISTS schema_migrations"):
            self._record_write("bookkeeping")
            return _Result()
        if normalized.startswith("INSERT INTO schema_migrations"):
            version = int(params[0])
            self._state.applied.add(version)
            self._record_write(f"version:{version}")
            return _Result()
        self._record_write(normalized)
        return _Result()

    def close(self) -> None:
        assert not self._owns_lock

    def _record_write(self, value: str) -> None:
        assert self._state.advisory_lock.locked(), f"write escaped migration lock: {value}"
        self._state.writes.append(value)


class _Hub:
    dialect = "postgres"

    def __init__(self, state: _SchemaState, actor: str) -> None:
        self._state = state
        self._actor = actor

    @contextmanager
    def transaction(self) -> Iterator[_Connection]:
        yield _Connection(self._state, self._actor, lock_connection=False)

    def lock_connection(self) -> _Connection:
        return _Connection(self._state, self._actor, lock_connection=True)


def _runner(state: _SchemaState, actor: str, known_version: int) -> MigrationRunner:
    hub = _Hub(state, actor)
    runner = MigrationRunner(cast(HubDatabase, hub), autocommit_connection=hub.lock_connection)
    vars(runner)["_known_schema_version"] = MethodType(lambda self: known_version, runner)
    return runner


def test_newer_schema_fails_closed() -> None:
    state = _SchemaState(applied={999})
    runner = _runner(state, "old", known_version=998)

    with pytest.raises(
        MigrationUnsupportedError,
        match=("hub schema is v999 but this gobby build knows v998 — update gobby on this machine"),
    ):
        runner.apply_startup(
            baseline_already_applied=lambda: True,
            apply_baseline=lambda: pytest.fail("baseline write must not run"),
        )

    assert state.writes == []
    assert not state.advisory_lock.locked()


def test_guard_lives_on_shared_runtime_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.storage.hub import postgres as module

    calls: list[tuple[Callable[[], bool], Callable[[], None]]] = []

    class _FakeRunner:
        def __init__(self, hub: Any, *, autocommit_connection: Callable[[], Any]) -> None:
            assert autocommit_connection == hub._open_advisory_lock_connection

        def apply_startup(
            self,
            *,
            baseline_already_applied: Callable[[], bool],
            apply_baseline: Callable[[], None],
        ) -> None:
            calls.append((baseline_already_applied, apply_baseline))

    monkeypatch.setattr(module, "MigrationRunner", _FakeRunner)
    database = object.__new__(module.PostgresHubDatabase)

    database.apply_migrations()

    assert calls == [
        (database._postgres_baseline_already_applied, database._apply_postgres_baseline)
    ]


@pytest.mark.parametrize(
    "phase",
    [
        "head_inspection",
        "pending_discovery",
        "baseline_first_write",
        "transactional_application",
        "non_transactional_application",
        "no_pending_exit",
    ],
)
def test_enclosing_lock_serializes_every_schema_decision(
    phase: str,
    tmp_path: Path,
) -> None:
    baseline_pending = phase == "baseline_first_write"
    no_pending = phase == "no_pending_exit"
    initial_versions = set() if baseline_pending else ({1, 2} if no_pending else {1})
    state = _SchemaState(applied=initial_versions)
    new_runner = _runner(state, "new", known_version=2)
    old_runner = _runner(state, "old", known_version=1)
    migration_path = tmp_path / "002_race.sql"
    directive = "-- gobby:non-transactional\n" if phase == "non_transactional_application" else ""
    migration_path.write_text(f"{directive}SELECT 1;\n", encoding="utf-8")
    migration = Migration(version=2, name="race", path=migration_path)

    for runner in (new_runner, old_runner):
        vars(runner)["_discover_migrations"] = MethodType(
            lambda self: [] if no_pending else [migration], runner
        )
        vars(runner)["_validate_contiguous_chain"] = MethodType(
            lambda self, applied, pending: None, runner
        )

    reached_phase = Barrier(2)
    release_phase = Event()

    def pause() -> None:
        assert state.advisory_lock.locked()
        reached_phase.wait(timeout=5)
        assert state.attempts["old"].wait(timeout=5)
        release_phase.wait(timeout=5)

    original_read_head = new_runner._read_schema_head
    head_reads = 0

    def read_head(self: MigrationRunner, txn: Any) -> int:
        nonlocal head_reads
        head_reads += 1
        should_pause = (phase == "head_inspection" and head_reads == 1) or (
            phase == "no_pending_exit" and head_reads == 2
        )
        if should_pause:
            pause()
        return original_read_head(txn)

    vars(new_runner)["_read_schema_head"] = MethodType(read_head, new_runner)

    original_discover = new_runner._discover_migrations

    def discover(self: MigrationRunner) -> list[Migration]:
        if phase == "pending_discovery":
            pause()
        return original_discover()

    vars(new_runner)["_discover_migrations"] = MethodType(discover, new_runner)

    original_run = new_runner._run_migration

    def run_migration(self: MigrationRunner, txn: Any, item: Migration) -> None:
        if phase == "transactional_application":
            pause()
        original_run(txn, item)

    vars(new_runner)["_run_migration"] = MethodType(run_migration, new_runner)

    original_non_transactional = new_runner._apply_non_transactional

    def apply_non_transactional(self: MigrationRunner, item: Migration) -> None:
        if phase == "non_transactional_application":
            pause()
        original_non_transactional(item)

    vars(new_runner)["_apply_non_transactional"] = MethodType(apply_non_transactional, new_runner)

    def baseline_already_applied() -> bool:
        return bool(state.applied)

    def apply_baseline() -> None:
        assert state.advisory_lock.locked()
        if phase == "baseline_first_write":
            pause()
        state.applied.add(1)
        state.writes.append("baseline")

    with ThreadPoolExecutor(max_workers=2) as executor:
        new_future = executor.submit(
            new_runner.apply_startup,
            baseline_already_applied=baseline_already_applied,
            apply_baseline=apply_baseline,
        )
        reached_phase.wait(timeout=5)
        old_future = executor.submit(
            old_runner.apply_startup,
            baseline_already_applied=baseline_already_applied,
            apply_baseline=apply_baseline,
        )
        assert state.attempts["old"].wait(timeout=5)
        assert not old_future.done()
        release_phase.set()
        new_future.result(timeout=5)
        with pytest.raises(
            MigrationUnsupportedError,
            match="hub schema is v2 but this gobby build knows v1",
        ):
            old_future.result(timeout=5)

    assert state.applied == {1, 2}
    assert not state.advisory_lock.locked()
