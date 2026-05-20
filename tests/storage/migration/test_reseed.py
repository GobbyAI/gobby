from __future__ import annotations

import importlib
from typing import Any

import pytest

pytestmark = pytest.mark.unit


class _Row(dict[str, Any]):
    def __init__(self, *values: Any, **items: Any) -> None:
        super().__init__(items)
        self._values = values

    def __getitem__(self, key: object) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _PostgresSequences:
    def __init__(self) -> None:
        self.setvals: list[tuple[str, int, bool]] = []

    def execute(self, sql: object, params: object = ()) -> _Result:
        text = str(sql).lower()
        if "setval" in text:
            values = tuple(params)
            sequence_or_table = str(values[0])
            sequence_name = (
                sequence_or_table
                if sequence_or_table.endswith("_seq")
                else f"{sequence_or_table}_id_seq"
            )
            self.setvals.append((sequence_name, int(values[-2]), bool(values[-1])))
            return _Result([_Row(values[-2])])
        if "information_schema.columns" in text or "pg_get_serial_sequence" in text:
            return _Result(
                [
                    _Row("tasks", "id", "tasks_id_seq", table_name="tasks", column_name="id"),
                    _Row(
                        "empty_events",
                        "id",
                        "empty_events_id_seq",
                        table_name="empty_events",
                        column_name="id",
                    ),
                ]
            )
        if "max(" in text and "tasks" in text:
            return _Result([_Row(41, max_id=41)])
        if "max(" in text and "empty_events" in text:
            return _Result([_Row(None, max_id=None)])
        raise AssertionError(f"unexpected SQL: {sql}")


def test_reseed_identity_sequences_uses_max_id_and_empty_table_convention() -> None:
    reseed = importlib.import_module("gobby.storage.migration.reseed")
    target = _PostgresSequences()

    reseed.reseed_identity_sequences(target)

    assert target.setvals == [
        ("tasks_id_seq", 41, True),
        ("empty_events_id_seq", 1, False),
    ]


def test_expected_sequence_state_documents_validator_convention() -> None:
    reseed = importlib.import_module("gobby.storage.migration.reseed")

    assert reseed.expected_sequence_state(41) == (41, True)
    assert reseed.expected_sequence_state(None) == (1, False)
