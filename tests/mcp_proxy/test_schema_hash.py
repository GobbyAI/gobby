"""Tests for mcp_proxy/schema_hash.py."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.schema_hash import SchemaHashManager, SchemaHashRecord, compute_schema_hash

pytestmark = pytest.mark.unit

HASH_TIMESTAMP = datetime(2025, 1, 1, tzinfo=UTC)


def _hash_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 1,
        "server_name": "srv",
        "tool_name": "tool",
        "project_id": "proj",
        "schema_hash": "h1",
        "last_verified_at": HASH_TIMESTAMP,
        "created_at": HASH_TIMESTAMP,
        "updated_at": HASH_TIMESTAMP,
    }
    row.update(overrides)
    return row


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


def test_compute_schema_hash_includes_description() -> None:
    schema = {"type": "object", "properties": {}}

    old_hash = compute_schema_hash(schema, description="Old description")
    new_hash = compute_schema_hash(schema, description="New description")

    assert old_hash != new_hash


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
        last_verified_at=HASH_TIMESTAMP,
        created_at=HASH_TIMESTAMP,
        updated_at=HASH_TIMESTAMP,
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
    mock_db.fetchone.return_value = _hash_row()

    result = manager.store_hash("srv", "tool", "proj", "h1")
    assert result.schema_hash == "h1"
    mock_db.execute.assert_called_once()


def test_store_hash_retrieve_fails(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = None

    with pytest.raises(RuntimeError, match="Failed to retrieve hash"):
        manager.store_hash("srv", "tool", "proj", "h1")


def test_get_hash_found(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = _hash_row()

    result = manager.get_hash("srv", "tool", "proj")
    assert result is not None
    assert result.schema_hash == "h1"


def test_get_hash_not_found(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = None
    assert manager.get_hash("srv", "tool", "proj") is None


def test_get_hashes_for_server(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchall.return_value = [
        _hash_row(id=1, tool_name="t1", schema_hash="h1"),
        _hash_row(id=2, tool_name="t2", schema_hash="h2"),
    ]

    results = manager.get_hashes_for_server("srv", "proj")
    assert len(results) == 2


def test_needs_reindexing_no_stored(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = None
    assert manager.needs_reindexing("srv", "tool", "proj", {"type": "object"}) is True


def test_needs_reindexing_hash_changed(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchone.return_value = _hash_row(schema_hash="old_hash")
    assert manager.needs_reindexing("srv", "tool", "proj", {"type": "string"}) is True


def test_needs_reindexing_hash_same(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    schema: dict[str, Any] = {"type": "string"}
    h = compute_schema_hash(schema)
    mock_db.fetchone.return_value = _hash_row(schema_hash=h)
    assert manager.needs_reindexing("srv", "tool", "proj", schema) is False


def test_needs_reindexing_includes_description(
    manager: SchemaHashManager, mock_db: MagicMock
) -> None:
    schema: dict[str, Any] = {"type": "object"}
    mock_db.fetchone.return_value = _hash_row(
        schema_hash=compute_schema_hash(schema, description="Old description")
    )

    assert (
        manager.needs_reindexing(
            "srv",
            "tool",
            "proj",
            schema,
            current_description="Old description",
        )
        is False
    )
    assert (
        manager.needs_reindexing(
            "srv",
            "tool",
            "proj",
            schema,
            current_description="New description",
        )
        is True
    )


def test_check_tools_for_changes(manager: SchemaHashManager, mock_db: MagicMock) -> None:
    mock_db.fetchall.return_value = [
        _hash_row(
            id=1,
            tool_name="existing",
            schema_hash=compute_schema_hash({"type": "string"}),
        ),
        _hash_row(id=2, tool_name="changed", schema_hash="old_hash"),
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


def test_check_tools_hashes_internal_and_external_shapes_consistently(
    manager: SchemaHashManager, mock_db: MagicMock
) -> None:
    schema = {"type": "object", "properties": {}}
    definition_hash = compute_schema_hash(schema, description="Same description")
    mock_db.fetchall.return_value = [
        _hash_row(id=1, tool_name="internal", schema_hash=definition_hash),
        _hash_row(id=2, tool_name="external", schema_hash=definition_hash),
    ]
    tools = [
        {
            "name": "internal",
            "description": "Same description",
            "inputSchema": schema,
        },
        {
            "name": "external",
            "description": "Same description",
            "input_schema": schema,
        },
    ]

    result = manager.check_tools_for_changes("srv", "proj", tools)

    assert result["unchanged"] == ["internal", "external"]
    assert result["changed"] == []


def test_check_tools_preserves_empty_schema_for_both_key_shapes(
    manager: SchemaHashManager, mock_db: MagicMock
) -> None:
    definition_hash = compute_schema_hash({}, description="No arguments")
    mock_db.fetchall.return_value = [
        _hash_row(id=1, tool_name="internal", schema_hash=definition_hash),
        _hash_row(id=2, tool_name="external", schema_hash=definition_hash),
    ]
    tools = [
        {"name": "internal", "description": "No arguments", "inputSchema": {}},
        {"name": "external", "description": "No arguments", "input_schema": {}},
    ]

    result = manager.check_tools_for_changes("srv", "proj", tools)

    assert result["unchanged"] == ["internal", "external"]
    assert result["changed"] == []


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
    mock_db.execute.assert_called_once_with(
        "DELETE FROM tool_schema_hashes "
        "WHERE project_id = %s AND server_name = %s AND tool_name != ALL(%s)",
        ("proj", "srv", ["tool1", "tool2"]),
    )


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
