"""Tests for runtime PostgreSQL pool configuration plumbing."""

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.storage.hub.runtime import runtime_hub_database

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
        patch(
            "gobby.storage.hub.runtime.admitted_database_url",
            return_value=config.database_url,
        ) as admission,
        patch("gobby.storage.hub.postgres.PostgresHubDatabase") as database_class,
    ):
        database_class.return_value = MagicMock()
        with runtime_hub_database(apply_migrations=False) as result:
            assert result is database_class.return_value

    assert admission.call_args_list == [call(config.database_url)]
    assert database_class.call_args_list == [
        call(config.database_url, pool_config=pool_config),
    ]
    assert database_class.return_value.close.call_args_list == [call()]
