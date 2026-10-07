"""Restore releases a maintenance epoch that a restored hub backup carries."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

import gobby.storage.maintenance_epoch as maintenance
from gobby.storage.maintenance_epoch import release_restored_maintenance_epoch
from tests.fixtures.postgres import isolated_test_schema


@pytest.fixture
def restored_hub(postgres_database_url: str) -> Iterator[tuple[psycopg.Connection[Any], str]]:
    """A scratch schema standing in for a restored hub, with its scoped DSN."""
    with isolated_test_schema(postgres_database_url, "epochrestore") as schema:
        with psycopg.connect(postgres_database_url, autocommit=True, row_factory=dict_row) as admin:
            admin.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            yield admin, postgres_database_url + f"?options=-csearch_path%3D{schema}"


def _create_epoch_table(connection: psycopg.Connection[Any]) -> None:
    connection.execute(
        """
        CREATE TABLE maintenance_epochs (
            id UUID PRIMARY KEY,
            released_at TIMESTAMPTZ,
            released_by_command TEXT
        )
        """
    )


def test_restore_releases_the_open_epoch(
    restored_hub: tuple[psycopg.Connection[Any], str],
) -> None:
    connection, restored_dsn = restored_hub
    _create_epoch_table(connection)
    open_id, closed_id = uuid.uuid4(), uuid.uuid4()
    connection.execute(
        """
        INSERT INTO maintenance_epochs(id, released_at, released_by_command)
        VALUES (%s, NULL, NULL), (%s, '2026-01-01T00:00:00Z', 'release')
        """,
        (open_id, closed_id),
    )

    assert release_restored_maintenance_epoch(restored_dsn) == open_id

    rows = connection.execute(
        "SELECT id, released_at IS NOT NULL AS released, released_by_command "
        "FROM maintenance_epochs ORDER BY released_by_command"
    ).fetchall()
    assert rows == [
        {"id": closed_id, "released": True, "released_by_command": "release"},
        {"id": open_id, "released": True, "released_by_command": "restore"},
    ]
    assert release_restored_maintenance_epoch(restored_dsn) is None


def test_restore_without_an_epoch_table_releases_nothing(
    restored_hub: tuple[psycopg.Connection[Any], str],
) -> None:
    _connection, restored_dsn = restored_hub

    assert release_restored_maintenance_epoch(restored_dsn) is None


def test_guard_refusal_retries_discovery_through_the_superuser_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch_id = uuid.uuid4()
    attempts: list[str] = []

    def read_open_epoch(database_url: str) -> uuid.UUID | None:
        attempts.append(database_url)
        if "event_triggers=off" not in database_url:
            raise psycopg.OperationalError("maintenance epoch is active")
        return epoch_id

    monkeypatch.setattr(maintenance, "_read_open_epoch", read_open_epoch)

    assert maintenance._discover_open_epoch("postgresql://hub@localhost/gobby") == epoch_id
    assert len(attempts) == 2
    assert "options='-c event_triggers=off'" in attempts[1]


def test_discovery_reports_the_guard_refusal_when_the_bypass_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = psycopg.OperationalError("maintenance epoch is active")

    def read_open_epoch(database_url: str) -> uuid.UUID | None:
        if "event_triggers=off" in database_url:
            raise psycopg.OperationalError("permission denied to set parameter")
        raise refusal

    monkeypatch.setattr(maintenance, "_read_open_epoch", read_open_epoch)

    with pytest.raises(psycopg.OperationalError) as caught:
        maintenance._discover_open_epoch("postgresql://hub@localhost/gobby")
    assert caught.value is refusal
