"""Tests for mcp_proxy/schema_hash.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.schema_hash import SchemaHashManager, SchemaHashRecord, compute_schema_hash

pytestmark = pytest.mark.unit

# --- compute_schema_hash ---


def test_compute_schema_hash_none() -> None:
    h = compute_schema_hash(None)
    assert isinstance(h, str)
    assert len(h) == 16


def test_compute_schema_hash_empty_dict() -> None:
    h = compute_schema_hash({})
    assert isinstance(h, str)
    assert len(h) == 16


def test_compute_schema_hash_deterministic() -> None:
    schema: dict[str, Any] = {"type": "object", "properties": {"name": {"type": "string"}}}
    h1 = compute_schema_hash(schema)
    h2 = compute_schema_hash(schema)
    assert h1 == h2


def test_compute_schema_hash_key_order_independent() -> None:
    s1: dict[str, Any] = {"b": 2, "a": 1}
    s2: dict[str, Any] = {"a": 1, "b": 2}
    assert compute_schema_hash(s1) == compute_schema_hash(s2)


def test_compute_schema_hash_different_schemas() -> None:
    h1 = compute_schema_hash({"type": "string"})
    h2 = compute_schema_hash({"type": "integer"})
    assert h1 != h2


# --- SchemaHashRecord ---


def test_schema_hash_record_from_row() -> None:
    row = {
        "id": 1,
        "server_name": "srv",
        "tool_name": "tool",
        "project_id": "proj",
        "schema_hash": "abc123",
        "last_verified_at": "2025-01-01",
        "created_at": "2025-01-01",
        "updated_at": "2025-01-01",
    }
    record = SchemaHashRecord.from_row(row)
    assert record.server_name == "srv"
    assert record.tool_name == "tool"


def test_schema_hash_record_to_dict() -> None:
    record = SchemaHashRecord(
        id=1,
        server_name="srv",
        tool_name="tool",
        project_id="proj",
        schema_hash="hash",
        last_verified_at="t1",
        created_at="t1",
        updated_at="t1",
    )
    d = record.to_dict()
    assert d["server_name"] == "srv"
    assert d["schema_hash"] == "hash"


# --- SchemaHashManager ---


@pytest.fixture
def mock_db() -> MagicMock:
    db = MagicMock()
    return db


@pytest.fixture
def manager(mock_db: MagicMock) -> SchemaHashManager:
    return SchemaHashManager(mock_db)


def test_store_hash(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = {
        "id": 1,
        "server_name": "srv",
        "tool_name": "tool",
        "project_id": "proj",
        "schema_hash": "h1",
        "last_verified_at": "t",
        "created_at": "t",
        "updated_at": "t",
    }

    result = manager.store_hash("srv", "tool", "proj", "h1")
    assert result.schema_hash == "h1"
    mock_db.execute.assert_called_once()


def test_store_hash_retrieve_fails(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = None

    with pytest.raises(RuntimeError, match="Failed to retrieve hash"):
        manager.store_hash("srv", "tool", "proj", "h1")


def test_get_hash_found(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = {
        "id": 1,
        "server_name": "srv",
        "tool_name": "tool",
        "project_id": "proj",
        "schema_hash": "h1",
        "last_verified_at": "t",
        "created_at": "t",
        "updated_at": "t",
    }

    result = manager.get_hash("srv", "tool", "proj")
    assert result is not None
    assert result.schema_hash == "h1"


def test_get_hash_not_found(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = None
    assert manager.get_hash("srv", "tool", "proj") is None


def test_get_hashes_for_server(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchall.return_value = [
        {
            "id": 1,
            "server_name": "srv",
            "tool_name": "t1",
            "project_id": "proj",
            "schema_hash": "h1",
            "last_verified_at": "t",
            "created_at": "t",
            "updated_at": "t",
        },
        {
            "id": 2,
            "server_name": "srv",
            "tool_name": "t2",
            "project_id": "proj",
            "schema_hash": "h2",
            "last_verified_at": "t",
            "created_at": "t",
            "updated_at": "t",
        },
    ]

    results = manager.get_hashes_for_server("srv", "proj")
    assert len(results) == 2


def test_needs_reindexing_no_stored(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = None
    assert manager.needs_reindexing("srv", "tool", "proj", {"type": "object"}) is True


def test_needs_reindexing_hash_changed(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = {
        "id": 1,
        "server_name": "srv",
        "tool_name": "tool",
        "project_id": "proj",
        "schema_hash": "old_hash",
        "last_verified_at": "t",
        "created_at": "t",
        "updated_at": "t",
    }
    assert manager.needs_reindexing("srv", "tool", "proj", {"type": "string"}) is True


def test_needs_reindexing_hash_same(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    schema: dict[str, Any] = {"type": "string"}
    h = compute_schema_hash(schema)
    mock_db.fetchone.return_value = {
        "id": 1,
        "server_name": "srv",
        "tool_name": "tool",
        "project_id": "proj",
        "schema_hash": h,
        "last_verified_at": "t",
        "created_at": "t",
        "updated_at": "t",
    }
    assert manager.needs_reindexing("srv", "tool", "proj", schema) is False


def test_check_tools_for_changes(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchall.return_value = [
        {
            "id": 1,
            "server_name": "srv",
            "tool_name": "existing",
            "project_id": "proj",
            "schema_hash": compute_schema_hash({"type": "string"}),
            "last_verified_at": "t",
            "created_at": "t",
            "updated_at": "t",
        },
        {
            "id": 2,
            "server_name": "srv",
            "tool_name": "changed",
            "project_id": "proj",
            "schema_hash": "old_hash",
            "last_verified_at": "t",
            "created_at": "t",
            "updated_at": "t",
        },
    ]

    tools = [
        {"name": "existing", "inputSchema": {"type": "string"}},
        {"name": "changed", "inputSchema": {"type": "integer"}},
        {"name": "brand_new", "inputSchema": {}},
    ]

    result = manager.check_tools_for_changes("srv", "proj", tools)
    assert "existing" in result["unchanged"]
    assert "changed" in result["changed"]
    assert "brand_new" in result["new"]


def test_check_tools_input_schema_key(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchall.return_value = []
    tools = [{"name": "t1", "input_schema": {"type": "object"}}]
    result = manager.check_tools_for_changes("srv", "proj", tools)
    assert "t1" in result["new"]


def test_update_verification_time(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    cursor = MagicMock()
    cursor.rowcount = 1
    mock_db.execute.return_value = cursor

    assert manager.update_verification_time("srv", "tool", "proj") is True


def test_update_verification_time_not_found(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    cursor = MagicMock()
    cursor.rowcount = 0
    mock_db.execute.return_value = cursor

    assert manager.update_verification_time("srv", "tool", "proj") is False


def test_delete_hash(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    cursor = MagicMock()
    cursor.rowcount = 1
    mock_db.execute.return_value = cursor

    assert manager.delete_hash("srv", "tool", "proj") is True


def test_delete_hash_not_found(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    cursor = MagicMock()
    cursor.rowcount = 0
    mock_db.execute.return_value = cursor

    assert manager.delete_hash("srv", "tool", "proj") is False


def test_delete_hashes_for_server(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    cursor = MagicMock()
    cursor.rowcount = 5
    mock_db.execute.return_value = cursor

    assert manager.delete_hashes_for_server("srv", "proj") == 5


def test_cleanup_stale_hashes(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    cursor = MagicMock()
    cursor.rowcount = 2
    mock_db.execute.return_value = cursor

    result = manager.cleanup_stale_hashes("srv", "proj", ["tool1", "tool2"])
    assert result == 2


def test_cleanup_stale_hashes_empty_valid(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    cursor = MagicMock()
    cursor.rowcount = 3
    mock_db.execute.return_value = cursor

    result = manager.cleanup_stale_hashes("srv", "proj", [])
    assert result == 3


def test_get_stats_with_project(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = {"count": 10}
    mock_db.fetchall.return_value = [
        {"server_name": "srv1", "count": 5},
        {"server_name": "srv2", "count": 5},
    ]

    stats = manager.get_stats("proj")
    assert stats["total_hashes"] == 10
    assert stats["by_server"]["srv1"] == 5


def test_get_stats_no_project(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = {"count": 20}
    mock_db.fetchall.return_value = []

    stats = manager.get_stats()
    assert stats["total_hashes"] == 20


def test_get_stats_no_rows(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = None
    mock_db.fetchall.return_value = []

    stats = manager.get_stats("proj")
    assert stats["total_hashes"] == 0
