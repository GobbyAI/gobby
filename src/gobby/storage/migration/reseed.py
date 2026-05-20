"""PostgreSQL sequence reseeding for SQLite imports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from psycopg import sql


class _Executable(Protocol):
    def execute(self, query: Any, params: Sequence[Any] = ()) -> Any: ...


@dataclass(frozen=True)
class IdentitySequence:
    table_schema: str
    table_name: str
    column_name: str
    sequence_name: str


@dataclass(frozen=True)
class SequenceReseedResult:
    table_schema: str
    table_name: str
    column_name: str
    sequence_name: str
    max_id: int | None
    last_value: int
    is_called: bool


class SequenceReseedError(RuntimeError):
    """Raised when PostgreSQL identity metadata is internally inconsistent."""


_IDENTITY_SEQUENCE_SQL = """
WITH sequence_columns AS (
    SELECT
        c.table_schema,
        c.table_name,
        c.column_name,
        c.ordinal_position,
        c.is_identity,
        pg_get_serial_sequence(
            format('%I.%I', c.table_schema, c.table_name),
            c.column_name
        ) AS sequence_name
    FROM information_schema.columns AS c
    WHERE c.table_schema = current_schema()
)
SELECT table_schema, table_name, column_name, sequence_name
FROM sequence_columns
WHERE is_identity = 'YES' OR sequence_name IS NOT NULL
ORDER BY table_schema, table_name, ordinal_position
"""


def expected_sequence_state(max_id: int | None) -> tuple[int, bool]:
    """Return the canonical sequence state for a table's current max identity value."""
    if max_id is None:
        return 1, False
    return max_id, True


def discover_identity_sequences(target: _Executable) -> list[IdentitySequence]:
    """Find identity/serial-backed columns in the target schema."""
    rows = target.execute(_IDENTITY_SEQUENCE_SQL).fetchall()
    sequences: list[IdentitySequence] = []
    for row in rows:
        table_schema, table_name, column_name, sequence_name = _identity_sequence_row(row)
        if sequence_name is None:
            raise SequenceReseedError(
                f"identity column {table_schema}.{table_name}.{column_name} has no sequence"
            )
        sequences.append(
            IdentitySequence(
                table_schema=table_schema,
                table_name=table_name,
                column_name=column_name,
                sequence_name=str(sequence_name),
            )
        )
    return sequences


def reseed_identity_sequences(target: _Executable) -> list[SequenceReseedResult]:
    """Set every identity/serial sequence to match the target table's data."""
    results: list[SequenceReseedResult] = []
    for sequence in discover_identity_sequences(target):
        max_id = _max_identity_value(target, sequence)
        last_value, is_called = expected_sequence_state(max_id)
        target.execute(
            "SELECT setval(%s::regclass, %s, %s)",
            (sequence.sequence_name, last_value, is_called),
        )
        results.append(
            SequenceReseedResult(
                table_schema=sequence.table_schema,
                table_name=sequence.table_name,
                column_name=sequence.column_name,
                sequence_name=sequence.sequence_name,
                max_id=max_id,
                last_value=last_value,
                is_called=is_called,
            )
        )
    return results


def _max_identity_value(target: _Executable, sequence: IdentitySequence) -> int | None:
    row = target.execute(
        sql.SQL("SELECT MAX({}) AS max_id FROM {}").format(
            sql.Identifier(sequence.column_name),
            sql.Identifier(sequence.table_schema, sequence.table_name),
        )
    ).fetchone()
    max_id = None if row is None else _row_value(row, "max_id", 0)
    if max_id is None:
        return None
    return int(max_id)


def _identity_sequence_row(row: Any) -> tuple[str, str, str, Any]:
    named_schema = _named_row_value(row, "table_schema")
    if named_schema is None and _named_row_value(row, "table_name") is not None:
        return (
            "public",
            str(_row_value(row, "table_name", 0)),
            str(_row_value(row, "column_name", 1)),
            _row_value(row, "sequence_name", 2),
        )
    return (
        str(_row_value(row, "table_schema", 0)),
        str(_row_value(row, "table_name", 1)),
        str(_row_value(row, "column_name", 2)),
        _row_value(row, "sequence_name", 3),
    )


def _named_row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return None


def _row_value(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]
