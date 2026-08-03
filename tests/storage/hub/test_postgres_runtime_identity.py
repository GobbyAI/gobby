"""Tests for PostgreSQL runtime-role pool configuration and checkout checks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.hub import postgres_pool
from gobby.storage.hub.postgres import PostgresHubDatabase


def test_runtime_pool_installs_configure_and_checkout_callbacks() -> None:
    with patch("gobby.storage.hub.postgres.ConnectionPool") as pool_class:
        database = PostgresHubDatabase(
            "postgresql://gobby:secret@localhost/gobby",
            runtime_role="gobby_daemon_runtime",
        )
        try:
            kwargs = pool_class.call_args.kwargs
            assert callable(kwargs["configure"])
            assert callable(kwargs["check"])
        finally:
            database.close()


def test_configure_runtime_role_quotes_the_fixed_identifier() -> None:
    connection = MagicMock()
    postgres_pool.configure_runtime_role(connection, "gobby_daemon_runtime")
    query = connection.execute.call_args.args[0]
    assert query.as_string(None) == 'SET ROLE "gobby_daemon_runtime"'
    connection.commit.assert_called_once_with()


def test_checkout_assertion_rejects_and_closes_wrong_identity() -> None:
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = ("gobby_agent_issuer",)

    with pytest.raises(postgres_pool.RuntimeRoleMismatchError, match="gobby_daemon_runtime"):
        postgres_pool.assert_runtime_role(connection, "gobby_daemon_runtime")

    connection.close.assert_called_once_with()


def test_checkout_assertion_accepts_runtime_identity() -> None:
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = ("gobby_daemon_runtime",)

    postgres_pool.assert_runtime_role(connection, "gobby_daemon_runtime")

    connection.commit.assert_called_once_with()
    connection.close.assert_not_called()


def test_runtime_role_identifier_is_validated() -> None:
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        PostgresHubDatabase(
            "postgresql://gobby:secret@localhost/gobby",
            runtime_role='gobby_daemon_runtime"; RESET ROLE; --',
        )
