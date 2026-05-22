"""Tests for one-shot Neo4j to FalkorDB config migration."""

from __future__ import annotations

import json

import pytest

from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase

pytestmark = pytest.mark.unit


def _seed_neo4j_config(db: LocalDatabase) -> None:
    store = ConfigStore(db)
    store.set("databases.neo4j.url", "http://localhost:8474", source="test")
    store.set("databases.neo4j.host", "localhost", source="test")
    store.set("databases.neo4j.port", 8474, source="test")
    store.set("databases.neo4j.database", "neo4j", source="test")
    store.set("databases.neo4j.graph_search", False, source="test")
    store.set("databases.neo4j.graph_min_score", 0.81, source="test")
    store.set("databases.neo4j.rrf_k", 17, source="test")
    store.set("databases.neo4j.graph_name", "custom-graph", source="test")

    db.execute(
        """
        INSERT INTO config_store (key, value, source, is_secret, updated_at)
        VALUES (?, ?, ?, 1, datetime('now'))
        """,
        ("databases.neo4j.auth", json.dumps("$secret:auth"), "test"),
    )
    db.execute(
        """
        INSERT INTO secrets (id, name, encrypted_value, category, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        ("secret-auth", "auth", "encrypted", "general", "legacy Neo4j auth"),
    )


def test_migration_moves_backend_agnostic_tunables_and_drops_neo4j_keys(
    temp_db: LocalDatabase,
) -> None:
    from gobby.storage.migrations import migrate_neo4j_config_to_falkordb

    _seed_neo4j_config(temp_db)

    migrate_neo4j_config_to_falkordb(temp_db)

    store = ConfigStore(temp_db)
    assert store.get("databases.falkordb.graph_search") is False
    assert store.get("databases.falkordb.graph_min_score") == 0.81
    assert store.get("databases.falkordb.rrf_k") == 17
    assert store.get("databases.falkordb.graph_name") == "custom-graph"

    assert store.list_keys(prefix="databases.neo4j.") == []
    assert store.get("databases.falkordb.url") is None
    assert store.get("databases.falkordb.auth") is None
    assert store.get("databases.falkordb.database") is None
    assert temp_db.fetchone("SELECT 1 FROM secrets WHERE name = ?", ("auth",)) is None


def test_migration_preserves_auth_secret_when_other_config_reference_survives(
    temp_db: LocalDatabase,
) -> None:
    from gobby.storage.migrations import migrate_neo4j_config_to_falkordb

    _seed_neo4j_config(temp_db)
    ConfigStore(temp_db).set("mock.test.auth", "$secret:auth", source="test")

    migrate_neo4j_config_to_falkordb(temp_db)

    assert (
        temp_db.fetchone(
            "SELECT 1 FROM config_store WHERE key = ?",
            ("databases.neo4j.auth",),
        )
        is None
    )
    assert temp_db.fetchone("SELECT value FROM config_store WHERE key = ?", ("mock.test.auth",))[
        "value"
    ] == json.dumps("$secret:auth")
    assert temp_db.fetchone("SELECT 1 FROM secrets WHERE name = ?", ("auth",)) is not None
