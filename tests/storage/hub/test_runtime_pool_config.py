"""Tests for runtime PostgreSQL pool configuration plumbing."""

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.storage.hub.runtime import apply_destructive_batch, runtime_hub_database
from gobby.storage.migrations import DestructiveMigrationContext

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


def _destructive_context() -> DestructiveMigrationContext:
    return DestructiveMigrationContext(
        epoch_id="4a8f0a1c-6d1b-4b3e-9d0f-2c7a6b5e1d34",
        batch_id="9b1d7c22-3e44-4f10-8a55-1c2d3e4f5a6b",
        manifest_sha256="a" * 64,
        backup_starting_head=354,
    )


def test_destructive_batch_runs_on_a_dedicated_pool_and_closes_it() -> None:
    pool_config = PostgresPoolConfig(min_size=1, max_size=2)
    context = _destructive_context()

    with patch("gobby.storage.hub.postgres.PostgresHubDatabase") as database_class:
        database_class.return_value = MagicMock()
        apply_destructive_batch("postgresql://gobby@localhost/gobby", pool_config, context)

    assert database_class.call_args_list == [
        call("postgresql://gobby@localhost/gobby", pool_config=pool_config),
    ]
    database = database_class.return_value
    assert database.apply_destructive_migrations.call_args_list == [call(context)]
    assert database.close.call_args_list == [call()]


def test_destructive_batch_closes_the_pool_when_the_batch_raises() -> None:
    pool_config = PostgresPoolConfig(min_size=1, max_size=2)

    with patch("gobby.storage.hub.postgres.PostgresHubDatabase") as database_class:
        database_class.return_value = MagicMock()
        database_class.return_value.apply_destructive_migrations.side_effect = RuntimeError(
            "migration 356 failed"
        )
        with pytest.raises(RuntimeError, match="migration 356 failed"):
            apply_destructive_batch(
                "postgresql://gobby@localhost/gobby",
                pool_config,
                _destructive_context(),
            )

    assert database_class.return_value.close.call_args_list == [call()]
