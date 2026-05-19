from __future__ import annotations

import importlib
import inspect

import pytest

pytestmark = pytest.mark.unit


def _postgres_module():
    return importlib.import_module("gobby.storage.hub.postgres")


def test_postgres_hub_database_exposes_backend_neutral_surface() -> None:
    module = _postgres_module()

    assert module.PostgresHubDatabase.dialect == "postgres"

    transaction = inspect.signature(module.PostgresHubDatabase.transaction)
    assert list(transaction.parameters) == ["self"]

    transaction_immediate = inspect.signature(module.PostgresHubDatabase.transaction_immediate)
    assert list(transaction_immediate.parameters) == ["self", "lock"]

    for method in ("apply_migrations", "close"):
        assert hasattr(module.PostgresHubDatabase, method), method


@pytest.mark.parametrize(
    ("sql", "params", "expected_sql", "expected_params"),
    [
        ("SELECT $1, $2", ("a", "b"), "SELECT %s, %s", ("a", "b")),
        (
            "WHERE a = $2 AND b = $1",
            ("x", "y"),
            "WHERE a = %s AND b = %s",
            ("y", "x"),
        ),
        ("WHERE a = $1 OR b = $1", ("z",), "WHERE a = %s OR b = %s", ("z", "z")),
        (
            "WHERE id IN ($1, $2, $3)",
            (1, 2, 3),
            "WHERE id IN (%s, %s, %s)",
            (1, 2, 3),
        ),
        ("SELECT $3, $1", ("a", "b", "c"), "SELECT %s, %s", ("c", "a")),
        (
            "CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM 1; END; $$ LANGUAGE plpgsql",
            (),
            "CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM 1; END; $$ LANGUAGE plpgsql",
            (),
        ),
        (
            "CREATE FUNCTION f(x int) RETURNS int AS $$ BEGIN RETURN $1 + 1; END; "
            "$$ LANGUAGE plpgsql",
            ("ignored",),
            "CREATE FUNCTION f(x int) RETURNS int AS $$ BEGIN RETURN $1 + 1; END; "
            "$$ LANGUAGE plpgsql",
            (),
        ),
        (
            "CREATE FUNCTION f(x int) RETURNS int AS $body$ BEGIN RETURN $1 + 1; END; "
            "$body$ LANGUAGE plpgsql",
            ("ignored",),
            "CREATE FUNCTION f(x int) RETURNS int AS $body$ BEGIN RETURN $1 + 1; END; "
            "$body$ LANGUAGE plpgsql",
            (),
        ),
        (
            "INSERT INTO t(name) VALUES ($1) WHERE id = "
            "(SELECT id FROM (SELECT $$abc$$, $2 AS k) s)",
            ("name", 42),
            "INSERT INTO t(name) VALUES (%s) WHERE id = "
            "(SELECT id FROM (SELECT $$abc$$, %s AS k) s)",
            ("name", 42),
        ),
        ("SELECT '$1', $1", ("bound",), "SELECT '$1', %s", ("bound",)),
        (
            "WHERE name = 'O''Brien' AND id = $1",
            (7,),
            "WHERE name = 'O''Brien' AND id = %s",
            (7,),
        ),
        (
            "-- $1 should not bind\nSELECT $1",
            ("bound",),
            "-- $1 should not bind\nSELECT %s",
            ("bound",),
        ),
        (
            "/* $1 should not bind */ SELECT $1",
            ("bound",),
            "/* $1 should not bind */ SELECT %s",
            ("bound",),
        ),
        ("SELECT foo$1, $1 FROM t", ("a",), "SELECT foo$1, %s FROM t", ("a",)),
    ],
)
def test_remap_placeholders_to_psycopg(sql, params, expected_sql, expected_params) -> None:
    module = _postgres_module()

    assert module._remap_placeholders_to_psycopg(sql, params) == (
        expected_sql,
        expected_params,
    )


def test_remap_placeholders_to_psycopg_rejects_out_of_range_index() -> None:
    module = _postgres_module()

    with pytest.raises(ValueError, match=r"\$3"):
        module._remap_placeholders_to_psycopg("SELECT $3", ("only",))


def test_remap_placeholders_to_psycopg_rejects_unterminated_dollar_quote() -> None:
    module = _postgres_module()

    with pytest.raises(ValueError, match="unterminated dollar-quote"):
        module._remap_placeholders_to_psycopg("SELECT $body$not closed", ())


class _FakePostgresConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.executemany_calls: list[tuple[str, list[tuple[object, ...]]]] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()):
        self.execute_calls.append((sql, tuple(params)))
        return object()

    def executemany(self, sql: str, rows) -> None:
        self.executemany_calls.append((sql, [tuple(row) for row in rows]))


def test_postgres_transaction_execute_remaps_before_driver_call() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute("SELECT $2, $1, '$3'", ("one", "two", "literal"))

    assert conn.execute_calls == [("SELECT %s, %s, '$3'", ("two", "one"))]


def test_postgres_transaction_executemany_reuses_first_row_rewrite(monkeypatch) -> None:
    module = _postgres_module()
    calls: list[tuple[str, tuple[object, ...]]] = []
    original = module._remap_placeholders_to_psycopg

    def wrapped(sql: str, params):
        calls.append((sql, tuple(params)))
        return original(sql, params)

    monkeypatch.setattr(module, "_remap_placeholders_to_psycopg", wrapped)

    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)
    tx.executemany(
        "INSERT INTO remap_rows (a, b, c) VALUES ($2, $1, $2)",
        [("left", "right"), ("first", "second")],
    )

    assert calls == [
        (
            "INSERT INTO remap_rows (a, b, c) VALUES ($2, $1, $2)",
            ("left", "right"),
        )
    ]
    assert conn.executemany_calls == [
        (
            "INSERT INTO remap_rows (a, b, c) VALUES (%s, %s, %s)",
            [("right", "left", "right"), ("second", "first", "second")],
        )
    ]


def test_postgres_transaction_executemany_empty_rows_skips_driver_and_remapper(monkeypatch) -> None:
    module = _postgres_module()

    def fail_if_called(sql: str, params):
        raise AssertionError("remapper should not run for empty executemany rows")

    monkeypatch.setattr(module, "_remap_placeholders_to_psycopg", fail_if_called)

    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)
    tx.executemany("INSERT INTO remap_rows (a) VALUES ($1)", [])

    assert conn.executemany_calls == []


def test_postgres_transaction_executemany_rejects_first_row_out_of_range() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    with pytest.raises(ValueError, match=r"\$2"):
        tx.executemany("INSERT INTO remap_rows (a) VALUES ($2)", [("only",)])

    assert conn.executemany_calls == []
