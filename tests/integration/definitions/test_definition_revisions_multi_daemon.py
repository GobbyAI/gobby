"""Two isolated daemons share one hub and observe definition-revision notifies."""

from __future__ import annotations

import multiprocessing as mp
import time
import uuid
from collections.abc import Iterator
from multiprocessing.connection import Connection
from typing import Any, cast

import psycopg
import pytest
from psycopg import sql

from gobby.storage.definitions.revisions import (
    DefinitionDomain,
    advance_persistent_revision,
)
from gobby.storage.hub.postgres import PostgresHubDatabase

_CREATE_REVISIONS_TABLE = """
CREATE TABLE definition_revisions (
    domain text PRIMARY KEY,
    revision bigint DEFAULT 0 NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
)
"""
_COMMAND_TIMEOUT = 15.0


def _scoped_url(database_url: str, schema: str) -> str:
    return f"{database_url}?options=-csearch_path%3D{schema}"


async def _serve_observer(connection: Connection, dsn: str) -> None:
    import asyncio

    from gobby.storage.definitions.notifications import DefinitionRevisionListener
    from gobby.storage.definitions.revisions import (
        fetch_persistent_revisions,
        get_definitions_revision,
    )
    from gobby.storage.hub.postgres import PostgresHubDatabase as ObserverDatabase

    database = ObserverDatabase(dsn)

    async def open_listen() -> Any:
        return await psycopg.AsyncConnection.connect(dsn, autocommit=True)

    listener = DefinitionRevisionListener(
        open_listen,
        fetch_revisions=lambda: fetch_persistent_revisions(database),
        poll_interval=0.05,
        reconnect_backoff=0.05,
    )

    await listener.start()
    connection.send({"ok": True, "result": {"ready": True}})
    try:
        while True:
            if not connection.poll():
                await asyncio.sleep(0.02)
                continue
            request = cast(dict[str, object], connection.recv())
            operation = request.get("operation")
            if operation == "revision":
                domain = cast(DefinitionDomain, request["domain"])
                connection.send(
                    {
                        "ok": True,
                        "result": {"revision": get_definitions_revision(domain)},
                    }
                )
            elif operation == "stop":
                connection.send({"ok": True, "result": {}})
                return
            else:
                connection.send({"ok": False, "kind": "error", "error": f"unknown {operation}"})
    finally:
        await listener.close()
        database.close()


def _observer_entry(connection: Connection, dsn: str) -> None:
    import asyncio

    asyncio.run(_serve_observer(connection, dsn))


class _ObserverWorker:
    def __init__(self, dsn: str) -> None:
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_observer_entry,
            args=(child, dsn),
            name="definition-revision-observer",
        )
        process.start()
        child.close()
        self._process = process
        self._connection = parent
        startup = self._receive()
        if not cast(bool, startup.get("ready")):
            self.stop()
            raise RuntimeError("observer worker failed to start")

    def _receive(self) -> dict[str, object]:
        if not self._connection.poll(_COMMAND_TIMEOUT):
            raise TimeoutError("observer worker did not respond")
        response = cast(dict[str, object], self._connection.recv())
        if not cast(bool, response["ok"]):
            raise RuntimeError(cast(str, response.get("error", "observer failed")))
        return cast(dict[str, object], response["result"])

    def revision(self, domain: DefinitionDomain) -> int:
        self._connection.send({"operation": "revision", "domain": domain})
        raw = self._receive()["revision"]
        if not isinstance(raw, int):
            raise TypeError(f"observer revision must be int, got {raw!r}")
        return raw

    def wait_for_revision(
        self, domain: DefinitionDomain, minimum: int, *, timeout: float = 5.0
    ) -> int:
        deadline = time.monotonic() + timeout
        latest = self.revision(domain)
        while time.monotonic() < deadline:
            if latest >= minimum:
                return latest
            time.sleep(0.02)
            latest = self.revision(domain)
        raise TimeoutError(f"observer did not reach {domain} revision {minimum}; last={latest}")

    def stop(self) -> None:
        try:
            self._connection.send({"operation": "stop"})
            self._connection.poll(2.0)
        except (BrokenPipeError, EOFError, OSError):
            pass
        self._process.join(timeout=5.0)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2.0)
        self._connection.close()


@pytest.fixture
def isolated_revision_dsn(postgres_database_url: str) -> Iterator[str]:
    schema = f"gobby_test_defrev_md_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(postgres_database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(_CREATE_REVISIONS_TABLE)
    try:
        yield _scoped_url(postgres_database_url, schema)
    finally:
        with psycopg.connect(postgres_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.integration
def test_definition_mutation_is_observed_by_second_daemon_without_restart(
    isolated_revision_dsn: str,
) -> None:
    observer = _ObserverWorker(isolated_revision_dsn)
    writer = PostgresHubDatabase(isolated_revision_dsn)
    try:
        assert observer.revision("rules") == 0
        with writer.transaction() as txn:
            advance_persistent_revision(txn, "rules")
        observed = observer.wait_for_revision("rules", 1)
        assert observed >= 1
        assert observer.revision("pipelines") == 0
    finally:
        writer.close()
        observer.stop()
