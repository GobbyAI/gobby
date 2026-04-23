"""Storage migration registry after flattening SQLite history."""

from collections.abc import Callable

from gobby.storage.database import LocalDatabase

MigrationAction = str | Callable[[LocalDatabase], None]

# Historical SQLite migrations through v219 are folded into baseline_schema.sql.
# Keep this empty until a future SQLite migration lands before PostgreSQL cutover.
MIGRATIONS: list[tuple[int, str, MigrationAction]] = []
