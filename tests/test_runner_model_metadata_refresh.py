from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import psycopg
import pytest
from psycopg.types.json import Jsonb

from gobby.llm.model_registry import ModelInfo, ModelReasoningInfo
from gobby.runner_lifecycle_shutdown import _cancel_periodic_tasks
from gobby.runner_model_metadata_refresh import (
    _metadata_rows,
    refresh_model_metadata_once,
    replace_model_metadata_async,
)
from gobby.storage.hub.async_ops import BoundedDBTimeoutError
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def _model() -> ModelInfo:
    return ModelInfo(
        id="openai/gpt-5.6-sol",
        name="GPT-5.6 Sol",
        context_length=258_400,
        max_completion_tokens=128_000,
    )


def test_metadata_rows_include_reasoning_fields() -> None:
    rows = _metadata_rows(
        [
            ModelInfo(
                id="openai/gpt-5.6-luna",
                name="GPT-5.6 Luna",
                context_length=1_050_000,
                max_completion_tokens=128_000,
                reasoning=ModelReasoningInfo(
                    supported_efforts=("max", "medium", "none"),
                    default_effort="medium",
                    default_enabled=True,
                    mandatory=False,
                ),
            )
        ]
    )

    row = rows[0]
    assert row[:4] == ("gpt-5.6-luna", 1_050_000, 128_000, True)
    assert isinstance(row[4], Jsonb)
    assert row[4].obj == ["max", "medium", "none"]
    assert row[5:] == ("medium", True, False, "registry")


class _CoverageAuditorSpy:
    def __init__(self) -> None:
        self.async_calls = 0

    def audit(self) -> None:
        return None

    async def audit_async(self) -> None:
        self.async_calls += 1


class _FakePGConn:
    def __init__(self, activity: list[str]) -> None:
        self.activity = activity
        self.finished = False

    def finish(self) -> None:
        if not self.finished:
            self.finished = True
            self.activity.append("hard-close")


class _FakeConnection:
    def __init__(self, *, block: str | None = None, delay: float = 0.0) -> None:
        self.activity: list[str] = []
        self.pgconn = _FakePGConn(self.activity)
        self.block = block
        self.delay = delay
        self.entered = asyncio.Event()
        self._cancel_count = 0

    async def execute(
        self,
        query: str | psycopg.sql.Composable,
        _params: object = None,
    ) -> None:
        query_text = query.as_string() if isinstance(query, psycopg.sql.Composable) else query
        self.activity.append(query_text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.block == "first-set" and query_text.startswith("SET LOCAL"):
            self.block = None
            await self._stubborn_wait()
        if self.block and self.block in query_text:
            await self._stubborn_wait()

    async def commit(self) -> None:
        self.activity.append("commit")

    async def close(self) -> None:
        self.activity.append("close")
        self.pgconn.finish()

    async def _stubborn_wait(self) -> None:
        self.entered.set()
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self._cancel_count += 1
                self.activity.append(f"cancel-{self._cancel_count}")
                if self._cancel_count >= 2:
                    raise


@pytest.mark.asyncio
async def test_replace_uses_bounded_four_statement_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    bounded_args: dict[str, object] = {}

    async def bounded(work: Any, **kwargs: object) -> int:
        bounded_args.update(kwargs)
        return await work(connection, asyncio.get_running_loop().time() + 5.0)

    monkeypatch.setattr("gobby.runner_model_metadata_refresh.run_bounded_db", bounded)

    inserted = await replace_model_metadata_async(
        SimpleNamespace(conninfo="postgresql://metadata"),
        [_model()],
    )

    assert inserted == 1
    assert bounded_args == {
        "conninfo": "postgresql://metadata",
        "deadline_seconds": 5.0,
        "statement_timeout_remaining": True,
    }
    assert connection.activity[0] == "DELETE FROM model_metadata"
    assert connection.activity[1].startswith("SET LOCAL statement_timeout = ")
    assert connection.activity[2].startswith("INSERT INTO model_metadata ")


@pytest.mark.asyncio
async def test_empty_refresh_retains_cache(caplog: pytest.LogCaptureFixture) -> None:
    database = cast(HubDatabase, SimpleNamespace(conninfo="postgresql://metadata"))
    auditor = _CoverageAuditorSpy()
    with (
        patch(
            "gobby.runner_model_metadata_refresh.fetch_models_async",
            new=AsyncMock(return_value=[]),
        ) as fetch,
        patch(
            "gobby.runner_model_metadata_refresh.replace_model_metadata_async",
            new=AsyncMock(),
        ) as replace,
    ):
        assert await refresh_model_metadata_once(database, coverage_auditor=auditor) is False

    fetch.assert_awaited_once_with()
    replace.assert_not_awaited()
    assert auditor.async_calls == 0
    assert "returned no models; retaining cached metadata" in caplog.text


async def test_successful_refresh_logs_population_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = cast(HubDatabase, SimpleNamespace(conninfo="postgresql://metadata"))
    auditor = _CoverageAuditorSpy()
    with (
        patch(
            "gobby.runner_model_metadata_refresh.fetch_models_async",
            new=AsyncMock(return_value=[_model()]),
        ),
        patch(
            "gobby.runner_model_metadata_refresh.replace_model_metadata_async",
            new=AsyncMock(return_value=1),
        ),
        caplog.at_level(logging.DEBUG, logger="gobby.runner_model_metadata_refresh"),
    ):
        assert await refresh_model_metadata_once(database, coverage_auditor=auditor) is True

    refresh_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "Refreshed model metadata cache with 1 models"
    )
    assert refresh_record.levelno == logging.DEBUG
    assert auditor.async_calls == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_fetch_and_is_idempotent() -> None:
    entered = asyncio.Event()

    async def blocked_fetch() -> list[ModelInfo]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    database = SimpleNamespace(conninfo="postgresql://metadata")
    with patch(
        "gobby.runner_model_metadata_refresh.fetch_models_async",
        side_effect=blocked_fetch,
    ):
        task = asyncio.create_task(refresh_model_metadata_once(database))
        runner = SimpleNamespace(_model_metadata_refresh_task=task, _wiki_watcher=None)
        await entered.wait()
        await _cancel_periodic_tasks(runner)
        await _cancel_periodic_tasks(runner)

    assert task.cancelled()


def _install_connection(
    monkeypatch: pytest.MonkeyPatch,
    connection: _FakeConnection,
) -> None:
    async def connect(*_args: object, **_kwargs: object) -> _FakeConnection:
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["first-set", "DELETE", "INSERT"])
async def test_blocked_db_statements_terminate_without_post_teardown_activity(
    monkeypatch: pytest.MonkeyPatch,
    block: str,
) -> None:
    connection = _FakeConnection(block=block)
    _install_connection(monkeypatch, connection)
    monkeypatch.setattr(
        "gobby.runner_model_metadata_refresh.MODEL_METADATA_WRITE_TIMEOUT_SECONDS",
        1.2,
    )

    started = time.monotonic()
    with pytest.raises(BoundedDBTimeoutError):
        await replace_model_metadata_async(
            SimpleNamespace(conninfo="postgresql://metadata"),
            [_model()],
        )
    assert time.monotonic() - started < 1.5
    assert connection.activity.count("hard-close") == 1
    assert connection.pgconn.finished is True
    assert connection.activity.count("close") == 1


@pytest.mark.asyncio
async def test_cumulative_delete_insert_deadline_is_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(delay=0.075)
    _install_connection(monkeypatch, connection)
    monkeypatch.setattr(
        "gobby.runner_model_metadata_refresh.MODEL_METADATA_WRITE_TIMEOUT_SECONDS",
        1.2,
    )

    with pytest.raises(BoundedDBTimeoutError):
        await replace_model_metadata_async(
            SimpleNamespace(conninfo="postgresql://metadata"),
            [_model()],
        )

    assert "commit" not in connection.activity
    assert connection.activity.count("hard-close") == 1


@pytest.mark.asyncio
async def test_blocked_connect_terminates_without_running_db_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    cancel_count = 0

    async def blocked_connect(*_args: object, **_kwargs: object) -> _FakeConnection:
        nonlocal cancel_count
        entered.set()
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_count += 1
                if cancel_count >= 2:
                    raise

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", blocked_connect)
    monkeypatch.setattr(
        "gobby.runner_model_metadata_refresh.MODEL_METADATA_WRITE_TIMEOUT_SECONDS",
        1.2,
    )

    with pytest.raises(BoundedDBTimeoutError):
        await replace_model_metadata_async(
            SimpleNamespace(conninfo="postgresql://metadata"),
            [_model()],
        )

    assert entered.is_set()
    assert cancel_count == 2
