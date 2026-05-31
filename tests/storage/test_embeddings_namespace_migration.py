from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.migrations import _execute_sql_script

pytestmark = pytest.mark.unit


def test_embeddings_namespace_migration_is_idempotent(temp_db: HubDatabase) -> None:
    store = ConfigStore(temp_db)
    for key, value in {
        "embeddings.api_base": "http://localhost:1234/v1",
        "embeddings.model": "nomic-embed-text",
        "embeddings.dim": 768,
    }.items():
        temp_db.execute(
            """
            INSERT INTO config_store (key, value, source, is_secret, updated_at)
            VALUES (%s, %s, 'user', FALSE, NOW())
            """,
            (key, json.dumps(value)),
        )
    temp_db.execute(
        """
        INSERT INTO secrets (id, name, encrypted_value, category, description, created_at, updated_at)
        VALUES ('legacy-secret', 'api_key', 'encrypted-value', 'general', 'legacy', NOW(), NOW())
        """
    )
    temp_db.execute(
        """
        INSERT INTO config_store (key, value, source, is_secret, updated_at)
        VALUES (%s, %s, 'user', TRUE, NOW())
        """,
        ("embeddings.api_key", json.dumps("$secret:api_key")),
    )

    migration = (
        Path("src/gobby/storage/migrations/271_embeddings_namespace_to_ai_embeddings.sql")
    ).read_text(encoding="utf-8")
    for _ in range(2):
        with temp_db.transaction() as txn:
            _execute_sql_script(txn, migration)

    assert store.get("ai.embeddings.api_base") == "http://localhost:1234/v1"
    assert store.get("ai.embeddings.model") == "nomic-embed-text"
    assert store.get("ai.embeddings.dim") == 768
    assert store.get("ai.embeddings.api_key") == "$secret:embeddings_api_key"
    assert not any(key.startswith("embeddings.") for key in store.list_keys())

    rows = temp_db.fetchall(
        """
        SELECT key, value, is_secret
        FROM config_store
        WHERE key = 'ai.embeddings.api_key'
        """
    )
    assert rows == [
        {
            "key": "ai.embeddings.api_key",
            "value": json.dumps("$secret:embeddings_api_key"),
            "is_secret": True,
        }
    ]
    copied = temp_db.fetchone(
        "SELECT id, encrypted_value FROM secrets WHERE name = 'embeddings_api_key'"
    )
    expected_id = (
        "secret-"
        + hashlib.md5(b"legacy-secret:embeddings_api_key", usedforsecurity=False).hexdigest()
    )
    assert copied == {"id": expected_id, "encrypted_value": "encrypted-value"}


def test_embedding_provider_cleanup_migration_is_idempotent(temp_db: HubDatabase) -> None:
    store = ConfigStore(temp_db)
    for key in ("ai.embeddings.provider", "embeddings.provider"):
        temp_db.execute(
            """
            INSERT INTO config_store (key, value, source, is_secret, updated_at)
            VALUES (%s, %s, 'user', FALSE, NOW())
            """,
            (key, json.dumps("lmstudio")),
        )
    store.set("ai.embeddings.model", "nomic-embed-text")

    migration = (
        Path("src/gobby/storage/migrations/272_drop_embedding_provider_config.sql")
    ).read_text(encoding="utf-8")
    for _ in range(2):
        with temp_db.transaction() as txn:
            _execute_sql_script(txn, migration)

    assert store.get("ai.embeddings.model") == "nomic-embed-text"
    assert store.get("ai.embeddings.provider") is None
    assert store.get("embeddings.provider") is None
