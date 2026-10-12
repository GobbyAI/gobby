"""Exercise lease projection cleanup with real, isolated Psycopg connections."""

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from gobby.runtime_grants.lease_view import RuntimeLeaseView, current_lease

pytestmark = pytest.mark.integration


@pytest.fixture
def lease_connection(
    postgres_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        connection.execute(
            "CREATE TEMP TABLE deployment_runtime "
            "(deployment_token text, fencing_epoch text, grant_signing_secret text)"
        )

        def connect(conninfo: str, *, autocommit: bool) -> psycopg.Connection[tuple[Any, ...]]:
            assert conninfo == postgres_database_url
            assert autocommit
            return connection

        monkeypatch.setattr("gobby.runtime_grants.lease_view.psycopg.connect", connect)
        yield connection


def test_bind_closes_connection_and_publishes_authority(
    postgres_database_url: str, lease_connection: psycopg.Connection[tuple[Any, ...]]
) -> None:
    lease_connection.execute(
        "INSERT INTO deployment_runtime VALUES (%s, %s, %s)",
        ("test-deployment", "7", "test-signing-secret"),
    )
    view = RuntimeLeaseView(
        postgres_database_url, machine_id="test-machine", deployment_token="test-deployment"
    )
    try:
        view.bind()
        assert lease_connection.closed
        assert view.fencing_epoch == 7
        assert view.grant_signing_secret == "test-signing-secret"
        assert view.owns_live_lease()
        assert current_lease() is view
    finally:
        view.release()


@pytest.mark.parametrize(
    "failure,error",
    [
        ("missing-row", RuntimeError),
        ("empty-secret", RuntimeError),
        ("invalid-epoch", ValueError),
        ("query-error", psycopg.errors.UndefinedColumn),
    ],
)
def test_bind_closes_connection_on_failure(
    postgres_database_url: str,
    lease_connection: psycopg.Connection[tuple[Any, ...]],
    failure: str,
    error: type[Exception],
) -> None:
    if failure == "query-error":
        lease_connection.execute(
            "ALTER TABLE pg_temp.deployment_runtime DROP COLUMN grant_signing_secret"
        )
    elif failure != "missing-row":
        lease_connection.execute(
            "INSERT INTO deployment_runtime VALUES (%s, %s, %s)",
            (
                "test-deployment",
                "invalid" if failure == "invalid-epoch" else "7",
                "" if failure == "empty-secret" else "test-signing-secret",
            ),
        )
    view = RuntimeLeaseView(
        postgres_database_url, machine_id="test-machine", deployment_token="test-deployment"
    )
    with pytest.raises(error):
        view.bind()
    assert lease_connection.closed
    assert not view.owns_live_lease()
    assert current_lease() is not view
