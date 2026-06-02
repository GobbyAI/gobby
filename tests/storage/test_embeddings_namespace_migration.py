from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gobby.config.persistence import EmbeddingsConfig
from gobby.search.embeddings import is_embedding_configured
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.migrations import _execute_sql_script

pytestmark = pytest.mark.unit

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/storage/migrations"


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

    migration = (MIGRATIONS_DIR / "271_embeddings_namespace_to_ai_embeddings.sql").read_text(
        encoding="utf-8"
    )
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
        "secret-" + hashlib.md5(b"api_key:embeddings_api_key", usedforsecurity=False).hexdigest()
    )
    assert copied == {"id": expected_id, "encrypted_value": "encrypted-value"}


def test_embeddings_namespace_migration_preserves_existing_canonical_values(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    for key, value in {
        "embeddings.api_base": "http://legacy.local/v1",
        "embeddings.model": "legacy-model",
        "embeddings.dim": 768,
    }.items():
        temp_db.execute(
            """
            INSERT INTO config_store (key, value, source, is_secret, updated_at)
            VALUES (%s, %s, 'legacy', FALSE, NOW())
            """,
            (key, json.dumps(value)),
        )
    store.set_many(
        {
            "ai.embeddings.api_base": "http://canonical.local/v1",
            "ai.embeddings.model": "canonical-model",
            "ai.embeddings.dim": 1024,
        }
    )
    temp_db.execute(
        """
        INSERT INTO config_store (key, value, source, is_secret, updated_at)
        VALUES (%s, %s, 'test', TRUE, NOW())
        """,
        ("ai.embeddings.api_key", json.dumps("$secret:canonical_key")),
    )
    temp_db.execute(
        """
        INSERT INTO secrets (id, name, encrypted_value, category, description, created_at, updated_at)
        VALUES ('legacy-secret', 'embeddings_api_key', 'encrypted-value', 'general', 'legacy', NOW(), NOW())
        """
    )
    temp_db.execute(
        """
        INSERT INTO config_store (key, value, source, is_secret, updated_at)
        VALUES (%s, %s, 'legacy', TRUE, NOW())
        """,
        ("embeddings.api_key", json.dumps("$secret:embeddings_api_key")),
    )

    migration = (MIGRATIONS_DIR / "271_embeddings_namespace_to_ai_embeddings.sql").read_text(
        encoding="utf-8"
    )
    with temp_db.transaction() as txn:
        _execute_sql_script(txn, migration)

    assert store.get("ai.embeddings.api_base") == "http://canonical.local/v1"
    assert store.get("ai.embeddings.model") == "canonical-model"
    assert store.get("ai.embeddings.dim") == 1024
    assert store.get("ai.embeddings.api_key") == "$secret:canonical_key"
    assert not any(key.startswith("embeddings.") for key in store.list_keys())


def test_embeddings_namespace_migration_creates_config_for_secret_only_state(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    temp_db.execute(
        """
        INSERT INTO secrets (id, name, encrypted_value, category, description, created_at, updated_at)
        VALUES ('legacy-secret', 'embeddings_api_key', 'encrypted-value', 'general', 'legacy', NOW(), NOW())
        """
    )

    migration = (MIGRATIONS_DIR / "271_embeddings_namespace_to_ai_embeddings.sql").read_text(
        encoding="utf-8"
    )
    with temp_db.transaction() as txn:
        _execute_sql_script(txn, migration)

    assert store.get("ai.embeddings.api_key") == "$secret:embeddings_api_key"
    row = temp_db.fetchone(
        """
        SELECT key, is_secret
        FROM config_store
        WHERE key = 'ai.embeddings.api_key'
        """
    )
    assert row == {"key": "ai.embeddings.api_key", "is_secret": True}


def test_embeddings_namespace_migration_prefers_legacy_config_row_over_secret_fallback(
    temp_db: HubDatabase,
) -> None:
    temp_db.execute(
        """
        INSERT INTO secrets (id, name, encrypted_value, category, description, created_at, updated_at)
        VALUES ('canonical-secret', 'embeddings_api_key', 'encrypted-value', 'general', 'canonical', NOW(), NOW())
        """
    )
    temp_db.execute(
        """
        INSERT INTO config_store (key, value, source, is_secret, updated_at)
        VALUES (%s, %s, 'legacy-config', TRUE, NOW())
        """,
        ("embeddings.api_key", json.dumps("$secret:embeddings_api_key")),
    )

    migration = (MIGRATIONS_DIR / "271_embeddings_namespace_to_ai_embeddings.sql").read_text(
        encoding="utf-8"
    )
    with temp_db.transaction() as txn:
        _execute_sql_script(txn, migration)

    row = temp_db.fetchone(
        """
        SELECT key, source, value
        FROM config_store
        WHERE key = 'ai.embeddings.api_key'
        """
    )
    assert row == {
        "key": "ai.embeddings.api_key",
        "source": "legacy-config",
        "value": json.dumps("$secret:embeddings_api_key"),
    }


def test_embeddings_namespace_migration_skips_api_key_when_no_source_secret(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    migration = (MIGRATIONS_DIR / "271_embeddings_namespace_to_ai_embeddings.sql").read_text(
        encoding="utf-8"
    )

    with temp_db.transaction() as txn:
        _execute_sql_script(txn, migration)

    assert temp_db.fetchone("SELECT id FROM secrets WHERE name = 'embeddings_api_key'") is None
    assert (
        temp_db.fetchone(
            """
        SELECT key
        FROM config_store
        WHERE key = 'ai.embeddings.api_key'
        """
        )
        is None
    )

    emb_cfg = EmbeddingsConfig(
        api_base=store.get("ai.embeddings.api_base"),
        api_key=store.get("ai.embeddings.api_key"),
    )
    assert (
        is_embedding_configured(
            model=emb_cfg.model,
            api_key=emb_cfg.api_key,
            api_base=emb_cfg.api_base,
        )
        is False
    )


def test_embeddings_namespace_migration_skips_dangling_legacy_api_key_reference(
    temp_db: HubDatabase,
) -> None:
    store = ConfigStore(temp_db)
    temp_db.execute(
        """
        INSERT INTO config_store (key, value, source, is_secret, updated_at)
        VALUES (%s, %s, 'legacy-config', TRUE, NOW())
        """,
        ("embeddings.api_key", json.dumps("$secret:api_key")),
    )

    migration = (MIGRATIONS_DIR / "271_embeddings_namespace_to_ai_embeddings.sql").read_text(
        encoding="utf-8"
    )
    with temp_db.transaction() as txn:
        _execute_sql_script(txn, migration)

    assert store.get("ai.embeddings.api_key") is None
    assert temp_db.fetchone("SELECT id FROM secrets WHERE name = 'embeddings_api_key'") is None
    assert not any(key.startswith("embeddings.") for key in store.list_keys())


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

    migration = (MIGRATIONS_DIR / "272_drop_embedding_provider_config.sql").read_text(
        encoding="utf-8"
    )
    for _ in range(2):
        with temp_db.transaction() as txn:
            _execute_sql_script(txn, migration)

    assert store.get("ai.embeddings.model") == "nomic-embed-text"
    assert store.get("ai.embeddings.provider") is None
    assert store.get("embeddings.provider") is None
