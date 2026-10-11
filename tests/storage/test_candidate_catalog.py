"""Cutover's catalog test cluster must be reachable and physically isolated."""

from __future__ import annotations

from unittest.mock import Mock

import psycopg
import pytest

from gobby.storage.schema_contract import SchemaContractError, verify_catalog_test_cluster

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("test_id", ["isolated-cluster", "live-cluster"])
def test_cluster_identity_gate_is_read_only(monkeypatch: pytest.MonkeyPatch, test_id: str) -> None:
    hub, test = Mock(), Mock()
    for connection, cluster_id in ((hub, "live-cluster"), (test, test_id)):
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.execute.return_value.fetchone.return_value = (cluster_id,)
    connect = Mock(side_effect=[hub, test])
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setenv("GOBBY_SCHEMA_TEST_DATABASE_URL", "postgresql://localhost:60892/test")
    if test_id == "live-cluster":
        with pytest.raises(SchemaContractError, match="requires a separate PostgreSQL cluster"):
            verify_catalog_test_cluster("postgresql://localhost:60891/hub")
    else:
        verify_catalog_test_cluster("postgresql://localhost:60891/hub")
    assert connect.call_count == 2
    assert [call.args[0] for call in connect.call_args_list] == [
        "postgresql://localhost:60891/hub",
        "postgresql://localhost:60892/test",
    ]
    for connection in (hub, test):
        connection.execute.assert_called_once_with(
            "SELECT system_identifier::text FROM pg_control_system()"
        )
        connection.__exit__.assert_called_once()


def test_cluster_gate_refuses_missing_url_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_SCHEMA_TEST_DATABASE_URL", raising=False)
    connect = Mock()
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(SchemaContractError, match="set GOBBY_SCHEMA_TEST_DATABASE_URL") as error:
        verify_catalog_test_cluster("postgresql://localhost/hub")
    assert "Start the isolated test hub" in str(error.value)
    assert connect.call_count == 0


def test_cluster_gate_refuses_unreachable_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOBBY_SCHEMA_TEST_DATABASE_URL", "postgresql://localhost:60892/test")
    connect = Mock(side_effect=psycopg.OperationalError("connection refused"))
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(SchemaContractError, match="connection refused") as error:
        verify_catalog_test_cluster("postgresql://localhost/hub")
    assert "Start the isolated test hub or set GOBBY_SCHEMA_TEST_DATABASE_URL" in str(error.value)
    assert connect.call_count == 1
