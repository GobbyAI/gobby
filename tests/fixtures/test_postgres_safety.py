"""Safety checks for PostgreSQL test fixtures."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.fixtures.postgres import (
    _adapt_seed_rows,
    _cleanup_orphaned_schemas,
    _configured_postgres_database_url,
    _enforce_safe_test_schema,
    _schema_looks_test_only,
)

pytestmark = pytest.mark.unit


def test_schema_looks_test_only_accepts_test_schema_name() -> None:
    assert _schema_looks_test_only("gobby_test_12345_master_abcd")


def test_schema_looks_test_only_rejects_default_public_schema() -> None:
    assert not _schema_looks_test_only("public")


def test_enforce_safe_test_schema_fails_under_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

    with pytest.raises(pytest.fail.Exception, match="outside a gobby_test_"):
        _enforce_safe_test_schema("public")


def test_enforce_safe_test_schema_allows_test_schema_under_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

    assert _enforce_safe_test_schema("gobby_test_12345_master_abcd") is None


def test_configured_postgres_database_url_requires_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

    assert _configured_postgres_database_url() is None


def test_configured_postgres_database_url_reads_bootstrap_under_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.config import bootstrap

    database_url = "postgresql://user:pass@localhost:60891/gobby"
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setattr(
        bootstrap,
        "load_bootstrap",
        lambda **_kwargs: SimpleNamespace(database_url=database_url),
    )

    assert _configured_postgres_database_url() == database_url


def test_adapt_seed_rows_wraps_json_values() -> None:
    adapted = _adapt_seed_rows([({"mode": "auto"}, ["fast"], "plain")])

    assert adapted[0][2] == "plain"
    assert adapted[0][0].obj == {"mode": "auto"}
    assert adapted[0][1].obj == ["fast"]


def test_orphan_cleanup_holds_drop_lock_through_scan_and_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.fixtures.postgres as postgres_fixture

    events: list[str] = []
    connection = MagicMock()
    connection.__enter__.return_value = connection

    def execute(query: object, params: object | None = None) -> MagicMock:
        del params
        rendered = str(query)
        result = MagicMock()
        if "pg_advisory_unlock" in rendered:
            events.append("unlock")
        elif "pg_advisory_lock" in rendered:
            events.append("lock")
        elif "information_schema.schemata" in rendered:
            events.append("scan")
            result.fetchall.return_value = [("gobby_test_0_1_master_abc",)]
        elif "DROP SCHEMA" in rendered:
            events.append("drop")
        return result

    connection.execute.side_effect = execute
    monkeypatch.setattr(
        postgres_fixture.psycopg,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    _cleanup_orphaned_schemas("postgresql://test")

    assert events == ["lock", "scan", "drop", "unlock"]
