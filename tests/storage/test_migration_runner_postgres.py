from __future__ import annotations

import importlib

import pytest

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


def test_invalid_concurrent_index_lookup_uses_psycopg_safe_placeholders(
    postgres_db: HubDatabase,
) -> None:
    migrations = importlib.import_module("gobby.storage.migrations")

    with postgres_db.transaction() as txn:
        row = txn.execute(
            migrations._INVALID_CONCURRENT_INDEX_SQL,
            ("gobby_missing_concurrent_index",),
        ).fetchone()

    assert row is None
