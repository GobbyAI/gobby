"""Tests for runtime PostgreSQL pool configuration plumbing."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.storage.hub.runtime import open_runtime_hub_database

pytestmark = pytest.mark.unit


def test_runtime_database_receives_resolved_pool_config() -> None:
    pool_config = PostgresPoolConfig(
        min_size=3,
        max_size=13,
        acquire_timeout_seconds=4.5,
        open_timeout_seconds=15.0,
    )
    config = SimpleNamespace(
        hub_backend="postgres",
        database_url="postgresql://gobby:secret@localhost:60891/gobby",
        postgres_pool=pool_config,
    )

    with (
        patch("gobby.storage.hub.runtime.load_config", return_value=config),
        patch("gobby.storage.hub.postgres.PostgresHubDatabase") as database_class,
    ):
        database_class.return_value = MagicMock()
        result = open_runtime_hub_database(apply_migrations=False)

    assert result is database_class.return_value
    database_class.assert_called_once_with(config.database_url, pool_config=pool_config)
