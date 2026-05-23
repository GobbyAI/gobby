"""PostgreSQL-only migration helper for tests."""

from __future__ import annotations

from typing import Any

import pytest


def run_migrations(db: Any) -> int:
    """Apply migrations on PostgreSQL test hubs.

    Kept under tests so older test helpers can move off the removed
    gobby.storage.migrations.run_migrations API without recreating a SQLite
    bootstrap path.
    """
    if getattr(db, "dialect", None) != "postgres":
        pytest.skip("SQLite test migration bootstrap was removed; use postgres_db or hub_db.")
    db.apply_migrations()
    return 0
