from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar, Literal, get_args, get_origin, get_type_hints

import pytest

pytestmark = pytest.mark.unit


def _protocol_module():
    return importlib.import_module("gobby.storage.hub.protocol")


def test_protocol_exports_backend_neutral_surface() -> None:
    module = _protocol_module()

    for name in (
        "Row",
        "Cursor",
        "Savepoint",
        "Transaction",
        "HubDatabase",
        "LockTarget",
        "LockAcquisitionOrderError",
    ):
        assert hasattr(module, name), name

    assert issubclass(module.LockAcquisitionOrderError, RuntimeError)

    lock_hints = get_type_hints(module.LockTarget)
    assert get_origin(lock_hints["PRIORITY"]) is ClassVar
    assert get_args(lock_hints["PRIORITY"]) == (int,)

    hub_hints = get_type_hints(module.HubDatabase)
    assert get_origin(hub_hints["dialect"]) is Literal
    assert set(get_args(hub_hints["dialect"])) == {"sqlite", "postgres"}


def test_transaction_protocol_defines_expected_methods() -> None:
    module = _protocol_module()

    execute = inspect.signature(module.Transaction.execute)
    assert list(execute.parameters) == ["self", "sql", "params"]
    execute_hints = get_type_hints(module.Transaction.execute)
    assert execute_hints["sql"] is str
    assert {get_origin(option) for option in get_args(execute_hints["params"])} == {
        Sequence,
        Mapping,
    }

    executemany = inspect.signature(module.Transaction.executemany)
    assert list(executemany.parameters) == ["self", "sql", "rows"]

    savepoint = inspect.signature(module.Transaction.savepoint)
    assert list(savepoint.parameters) == ["self", "name"]

    after_commit = inspect.signature(module.Transaction.after_commit)
    assert list(after_commit.parameters) == ["self", "callback"]
    after_commit_hints = get_type_hints(module.Transaction.after_commit)
    assert get_origin(after_commit_hints["callback"]) is Callable

    acquire_additional_lock = inspect.signature(module.Transaction.acquire_additional_lock)
    assert list(acquire_additional_lock.parameters) == ["self", "lock"]
    acquire_hints = get_type_hints(module.Transaction.acquire_additional_lock)
    assert acquire_hints["lock"] is module.LockTarget

    transaction_hints = get_type_hints(module.Transaction)
    assert transaction_hints["is_immediate"] is bool


def test_cursor_and_savepoint_protocols_are_driver_neutral() -> None:
    module = _protocol_module()

    fetchone_hints = get_type_hints(module.Cursor.fetchone)
    fetchall_hints = get_type_hints(module.Cursor.fetchall)
    assert fetchone_hints["return"] == module.Row | None
    assert get_origin(fetchall_hints["return"]) is Sequence

    row_origin = get_origin(module.Row)
    row_args = get_args(module.Row)
    assert row_origin is Mapping
    assert row_args == (str, Any)

    assert list(inspect.signature(module.Savepoint.release).parameters) == ["self"]
    assert list(inspect.signature(module.Savepoint.rollback).parameters) == ["self"]


def test_hub_database_exposes_regular_and_immediate_transactions() -> None:
    module = _protocol_module()

    transaction = inspect.signature(module.HubDatabase.transaction)
    assert list(transaction.parameters) == ["self"]

    transaction_immediate = inspect.signature(module.HubDatabase.transaction_immediate)
    assert list(transaction_immediate.parameters) == ["self", "lock"]

    immediate_hints = get_type_hints(module.HubDatabase.transaction_immediate)
    assert immediate_hints["lock"] is module.LockTarget

    for method in ("apply_migrations", "close"):
        assert hasattr(module.HubDatabase, method), method


def test_protocol_annotations_do_not_leak_sqlite_types() -> None:
    module = _protocol_module()
    inspected = (
        module.Cursor,
        module.Savepoint,
        module.Transaction,
        module.HubDatabase,
        module.LockTarget,
    )

    rendered = "\n".join(
        f"{cls.__name__} {getattr(cls, '__annotations__', {})} "
        f"{[str(inspect.signature(member)) for _, member in inspect.getmembers(cls, inspect.isfunction)]}"
        for cls in inspected
    )

    assert "sqlite3" not in rendered
