"""Tests for PostgreSQL runtime-role pool configuration and checkout checks."""

from __future__ import annotations

from typing import cast
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
    connection.autocommit = False
    connection.execute.return_value.fetchone.return_value = ("gobby_agent_issuer",)

    with pytest.raises(postgres_pool.RuntimeRoleMismatchError, match="gobby_daemon_runtime"):
        postgres_pool.assert_runtime_role(connection, "gobby_daemon_runtime")

    connection.close.assert_called_once_with()


def test_checkout_assertion_accepts_runtime_identity() -> None:
    connection = MagicMock()
    connection.autocommit = False
    connection.execute.return_value.fetchone.return_value = ("gobby_daemon_runtime",)

    postgres_pool.assert_runtime_role(connection, "gobby_daemon_runtime")

    connection.close.assert_not_called()


def test_the_check_costs_one_round_trip_and_leaves_no_transaction_to_close() -> None:
    """The commit was two thirds of the check's cost and verified nothing.

    It existed only to close the transaction the check's own SELECT opened, so
    running that SELECT in autocommit removes it without changing what is
    verified. Every database operation in the daemon pays this on checkout
    (#20853).
    """
    connection = MagicMock()
    connection.autocommit = False
    connection.execute.return_value.fetchone.return_value = ("gobby_daemon_runtime",)
    modes: list[bool] = []

    def _record_mode(*args: object, **kwargs: object) -> MagicMock:
        modes.append(connection.autocommit)
        return cast(MagicMock, connection.execute.return_value)

    connection.execute.side_effect = _record_mode

    postgres_pool.assert_runtime_role(connection, "gobby_daemon_runtime")

    assert connection.execute.call_count == 1
    assert modes == [True], "the check's query must not open a transaction"
    connection.commit.assert_not_called()


@pytest.mark.parametrize(
    ("observed", "raises"),
    [("gobby_daemon_runtime", False), ("gobby_agent_issuer", True)],
    ids=["match", "mismatch"],
)
def test_the_check_restores_the_autocommit_mode_it_found(observed: str, raises: bool) -> None:
    """The pool hands the connection straight to a caller that expects
    transactions, so a check that left autocommit on would silently commit
    every later statement."""
    connection = MagicMock()
    connection.autocommit = False
    connection.execute.return_value.fetchone.return_value = (observed,)

    if raises:
        with pytest.raises(postgres_pool.RuntimeRoleMismatchError):
            postgres_pool.assert_runtime_role(connection, "gobby_daemon_runtime")
    else:
        postgres_pool.assert_runtime_role(connection, "gobby_daemon_runtime")

    assert connection.autocommit is False


def test_a_failing_query_still_restores_the_autocommit_mode() -> None:
    """A dead connection is the pool's to discard, but it must not be handed
    back mid-check with the mode changed underneath it."""
    connection = MagicMock()
    connection.autocommit = False
    connection.execute.side_effect = RuntimeError("connection lost")

    with pytest.raises(RuntimeError, match="connection lost"):
        postgres_pool.assert_runtime_role(connection, "gobby_daemon_runtime")

    assert connection.autocommit is False


def test_an_invalid_identifier_is_rejected_before_any_statement_runs() -> None:
    connection = MagicMock()
    connection.autocommit = False

    with pytest.raises(ValueError, match="invalid SQL identifier"):
        postgres_pool.assert_runtime_role(connection, 'runtime"; RESET ROLE; --')

    connection.execute.assert_not_called()
    assert connection.autocommit is False


def test_runtime_role_identifier_is_validated() -> None:
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        PostgresHubDatabase(
            "postgresql://gobby:secret@localhost/gobby",
            runtime_role='gobby_daemon_runtime"; RESET ROLE; --',
        )
