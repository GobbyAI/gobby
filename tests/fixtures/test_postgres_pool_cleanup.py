"""Regression tests for PostgreSQL fixture pool ownership."""

from unittest.mock import MagicMock

import pytest

from tests.fixtures import postgres as postgres_fixtures


def test_postgres_db_closes_pool_when_setup_reset_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixture setup failure cannot bypass its database owner cleanup."""
    database = MagicMock()
    database_type = MagicMock(return_value=database)
    reset_schema = MagicMock(side_effect=[RuntimeError("setup reset failed"), None])
    monkeypatch.setattr(postgres_fixtures, "PostgresHubDatabase", database_type)
    monkeypatch.setattr(postgres_fixtures, "_reset_schema", reset_schema)

    fixture = postgres_fixtures.postgres_db.__wrapped__(
        "postgresql://test:test@127.0.0.1:60892/test",
        "gobby_test_pool_cleanup",
        {},
    )

    with pytest.raises(RuntimeError, match="setup reset failed"):
        next(fixture)

    database.close.assert_called_once_with()
    assert reset_schema.call_count == 2
