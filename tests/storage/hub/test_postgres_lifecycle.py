from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from gobby.storage.hub import postgres

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, row: dict[str, str] | None = None) -> None:
        self._row = row

    def fetchone(self) -> dict[str, str] | None:
        return self._row


class _SettingsTransaction:
    def __init__(self) -> None:
        self.settings = {
            "statement_timeout": "2500ms",
            "lock_timeout": "750ms",
        }
        self.statements: list[tuple[str, tuple[str, ...]]] = []

    def execute(self, sql: str, params: tuple[str, ...] = ()) -> _Result:
        self.statements.append((sql, params))
        if "current_setting" in sql:
            return _Result(dict(self.settings))
        if "set_config('statement_timeout'" in sql:
            self.settings["statement_timeout"] = params[0]
        elif "set_config('lock_timeout'" in sql:
            self.settings["lock_timeout"] = params[0]
        else:
            raise AssertionError(f"unexpected query: {sql}")
        return _Result()


def _database_with_transaction(
    monkeypatch: pytest.MonkeyPatch,
    transaction: _SettingsTransaction,
) -> postgres.PostgresHubDatabase:
    database = object.__new__(postgres.PostgresHubDatabase)

    @contextmanager
    def transaction_context() -> Iterator[_SettingsTransaction]:
        yield transaction

    monkeypatch.setattr(database, "transaction", transaction_context)
    return database


def test_bounded_transaction_sets_bounds_before_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _SettingsTransaction()
    database = _database_with_transaction(monkeypatch, transaction)

    with database.bounded_transaction(statement_timeout_ms=100, lock_timeout_ms=50):
        assert transaction.settings == {
            "statement_timeout": "100ms",
            "lock_timeout": "50ms",
        }

    assert transaction.settings == {
        "statement_timeout": "2500ms",
        "lock_timeout": "750ms",
    }


@pytest.mark.parametrize(
    ("statement_timeout_ms", "lock_timeout_ms"),
    [(0, 1), (-1, 1), (1, 0), (1, -1)],
)
def test_bounded_transaction_rejects_nonpositive_bounds(
    statement_timeout_ms: int,
    lock_timeout_ms: int,
) -> None:
    database = object.__new__(postgres.PostgresHubDatabase)

    with pytest.raises(ValueError, match="positive milliseconds"):
        with database.bounded_transaction(
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        ):
            pass


def test_bounded_transaction_restores_bounds_after_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _SettingsTransaction()
    database = _database_with_transaction(monkeypatch, transaction)

    with pytest.raises(RuntimeError, match="body failed"):
        with database.bounded_transaction(statement_timeout_ms=100, lock_timeout_ms=50):
            raise RuntimeError("body failed")

    assert transaction.settings == {
        "statement_timeout": "2500ms",
        "lock_timeout": "750ms",
    }


def test_bounded_transaction_restores_nested_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _SettingsTransaction()
    database = _database_with_transaction(monkeypatch, transaction)

    with database.bounded_transaction(statement_timeout_ms=100, lock_timeout_ms=50):
        assert transaction.settings == {
            "statement_timeout": "100ms",
            "lock_timeout": "50ms",
        }
        with database.bounded_transaction(statement_timeout_ms=20, lock_timeout_ms=10):
            assert transaction.settings == {
                "statement_timeout": "20ms",
                "lock_timeout": "10ms",
            }
        assert transaction.settings == {
            "statement_timeout": "100ms",
            "lock_timeout": "50ms",
        }

    assert transaction.settings == {
        "statement_timeout": "2500ms",
        "lock_timeout": "750ms",
    }


def test_postgres_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    close_timeouts: list[float] = []

    class FakePool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def close(self, *, timeout: float) -> None:
            close_timeouts.append(timeout)

    monkeypatch.setattr(postgres, "ConnectionPool", FakePool)
    database = postgres.PostgresHubDatabase("postgresql://gobby:secret@localhost/gobby")

    database.close()
    database.close()

    assert close_timeouts == [postgres._POOL_CLOSE_TIMEOUT_SECONDS]


def test_postgres_open_after_close_raises_without_reopening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"open": 0, "close": 0}

    class FakePool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def open(self, *, wait: bool, timeout: float) -> None:
            calls["open"] += 1

        def close(self, *, timeout: float) -> None:
            calls["close"] += 1

    monkeypatch.setattr(postgres, "ConnectionPool", FakePool)
    database = postgres.PostgresHubDatabase("postgresql://gobby:secret@localhost/gobby")
    database.close()

    with pytest.raises(RuntimeError, match="connection pool is closed"):
        database.open()
    with pytest.raises(RuntimeError, match="connection pool is closed"):
        with database.transaction():
            pass

    assert calls == {"open": 0, "close": 1}
