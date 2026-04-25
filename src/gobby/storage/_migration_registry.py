"""Storage migration registry after flattening SQLite history."""

from collections.abc import Callable

from gobby.storage.database import LocalDatabase

MigrationAction = str | Callable[[LocalDatabase], None]

# Historical SQLite migrations through v219 are folded into baseline_schema.sql.
MIGRATIONS: list[tuple[int, str, MigrationAction]] = [
    (
        220,
        "Add terminal_reason to agent_runs",
        """
        ALTER TABLE agent_runs ADD COLUMN terminal_reason TEXT
        """,
    )
]
