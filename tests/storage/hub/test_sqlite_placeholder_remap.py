from __future__ import annotations

import importlib
from typing import ClassVar

import pytest

pytestmark = pytest.mark.unit


def _sqlite_module():
    return importlib.import_module("gobby.storage.hub.sqlite")


def _protocol_module():
    return importlib.import_module("gobby.storage.hub.protocol")


class _OuterLock:
    PRIORITY: ClassVar[int] = 100

    def __str__(self) -> str:
        return "outer-lock"


class _InnerLock:
    PRIORITY: ClassVar[int] = 200

    def __str__(self) -> str:
        return "inner-lock"


class _OutOfOrderLock:
    PRIORITY: ClassVar[int] = 50

    def __str__(self) -> str:
        return "out-of-order-lock"


@pytest.mark.parametrize(
    ("sql", "params", "expected_sql", "expected_params"),
    [
        ("SELECT $1, $2", ("a", "b"), "SELECT ?, ?", ("a", "b")),
        ("WHERE a = $2 AND b = $1", ("x", "y"), "WHERE a = ? AND b = ?", ("y", "x")),
        ("WHERE a = $1 OR b = $1", ("z",), "WHERE a = ? OR b = ?", ("z", "z")),
        ("WHERE id IN ($1, $2, $3)", (1, 2, 3), "WHERE id IN (?, ?, ?)", (1, 2, 3)),
        ("SELECT $3, $1", ("a", "b", "c"), "SELECT ?, ?", ("c", "a")),
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
            "INSERT INTO t(name) VALUES (?) WHERE id = (SELECT id FROM (SELECT $$abc$$, ? AS k) s)",
            ("name", 42),
        ),
        ("SELECT '$1', $1", ("bound",), "SELECT '$1', ?", ("bound",)),
        ("WHERE name = 'O''Brien' AND id = $1", (7,), "WHERE name = 'O''Brien' AND id = ?", (7,)),
        (
            "-- $1 should not bind\nSELECT $1",
            ("bound",),
            "-- $1 should not bind\nSELECT ?",
            ("bound",),
        ),
        (
            "/* $1 should not bind */ SELECT $1",
            ("bound",),
            "/* $1 should not bind */ SELECT ?",
            ("bound",),
        ),
        ('SELECT "$1", $1 FROM t', ("a",), 'SELECT "$1", ? FROM t', ("a",)),
        ("SELECT foo$1, $1 FROM t", ("a",), "SELECT foo$1, ? FROM t", ("a",)),
    ],
)
def test_remap_placeholders(sql, params, expected_sql, expected_params) -> None:
    module = _sqlite_module()

    assert module._remap_placeholders(sql, params) == (expected_sql, expected_params)


def test_remap_placeholders_rejects_out_of_range_index() -> None:
    module = _sqlite_module()

    with pytest.raises(ValueError, match=r"\$3"):
        module._remap_placeholders("SELECT $3", ("only",))


def test_remap_placeholders_rejects_unterminated_dollar_quote() -> None:
    module = _sqlite_module()

    with pytest.raises(ValueError, match="unterminated dollar-quote"):
        module._remap_placeholders("SELECT $body$not closed", ())


def test_sqlite_hub_database_executemany_uses_remapped_rows(tmp_path) -> None:
    module = _sqlite_module()
    db = module.SqliteHubDatabase(str(tmp_path / "hub.db"))

    try:
        with db.transaction() as tx:
            tx.execute("CREATE TABLE remap_rows (a TEXT NOT NULL, b TEXT NOT NULL)")
            tx.executemany(
                "INSERT INTO remap_rows (a, b) VALUES ($2, $1)",
                [("left", "right"), ("first", "second")],
            )
            rows = tx.execute("SELECT a, b FROM remap_rows ORDER BY a").fetchall()
    finally:
        db.close()

    assert all(isinstance(row, dict) for row in rows)
    assert rows == [
        {"a": "right", "b": "left"},
        {"a": "second", "b": "first"},
    ]


def test_sqlite_hub_database_exposes_backend_neutral_surface(tmp_path) -> None:
    module = _sqlite_module()
    db = module.SqliteHubDatabase(str(tmp_path / "hub.db"))

    try:
        assert db.dialect == "sqlite"
        for method in (
            "transaction",
            "transaction_immediate",
            "execute",
            "executemany",
            "fetchone",
            "fetchall",
            "safe_update",
            "apply_migrations",
            "close",
        ):
            assert hasattr(db, method), method

        with db.transaction() as tx:
            assert tx.is_immediate is False
            tx.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            tx.execute("INSERT INTO items (id, name) VALUES ($1, $2)", (1, "one"))
            row = tx.execute("SELECT id, name FROM items WHERE id = $1", (1,)).fetchone()
    finally:
        db.close()

    assert isinstance(row, dict)
    assert row == {"id": 1, "name": "one"}


def test_sqlite_transaction_immediate_enforces_nested_lock_priority(tmp_path) -> None:
    protocol = _protocol_module()
    module = _sqlite_module()
    db = module.SqliteHubDatabase(str(tmp_path / "hub.db"))

    try:
        with db.transaction_immediate(_OuterLock()) as outer_tx:
            assert outer_tx.is_immediate is True
            with db.transaction_immediate(_InnerLock()) as inner_tx:
                assert inner_tx.is_immediate is True

            with pytest.raises(protocol.LockAcquisitionOrderError) as exc_info:
                with db.transaction_immediate(_OutOfOrderLock()):
                    pass
    finally:
        db.close()

    message = str(exc_info.value)
    assert "200" in message
    assert "50" in message
    assert "inner-lock" in message
    assert "out-of-order-lock" in message
