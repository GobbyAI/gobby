"""Startup cleanup for legacy configuration rows."""

from __future__ import annotations

import json
import logging

from gobby.storage.hub.protocol import HubDatabase, Transaction

logger = logging.getLogger(__name__)

_LEGACY_NEO4J_CONFIG_PREFIX = "databases.neo4j."
_FALKORDB_CONFIG_PREFIX = "databases.falkordb."
_LEGACY_AUTH_SECRET_NAME = "auth"
_LEGACY_AUTH_SECRET_REF = json.dumps(f"$secret:{_LEGACY_AUTH_SECRET_NAME}")
_NEO4J_TUNABLE_KEYS = ("graph_search", "graph_min_score", "rrf_k", "graph_name")


def check_stale_neo4j_config(db: HubDatabase) -> None:
    """Detect and clear stale Neo4j config rows after migration-time cleanup."""
    stale_keys = _stale_neo4j_config_keys(db)
    if not stale_keys:
        return

    _log_stale_neo4j_config(stale_keys)
    with db.transaction() as txn:
        _copy_neo4j_tunables_to_falkordb(txn)
        _delete_neo4j_config_rows(txn)
        _delete_orphaned_legacy_auth_secret(txn)


def _stale_neo4j_config_keys(db: HubDatabase) -> tuple[str, ...]:
    rows = db.fetchall(
        "SELECT key FROM config_store WHERE key LIKE %s ORDER BY key",
        (f"{_LEGACY_NEO4J_CONFIG_PREFIX}%",),
    )
    return tuple(str(row["key"]) for row in rows)


def _log_stale_neo4j_config(stale_keys: tuple[str, ...]) -> None:
    logger.warning(
        "Detected stale Neo4j config keys (%s) - these are no longer used. "
        "Run `gobby install --falkordb` to set up FalkorDB. Cleaning them up now.",
        ", ".join(stale_keys),
    )


def _copy_neo4j_tunables_to_falkordb(txn: Transaction) -> None:
    for key in _NEO4J_TUNABLE_KEYS:
        stale_key = f"{_LEGACY_NEO4J_CONFIG_PREFIX}{key}"
        falkordb_key = f"{_FALKORDB_CONFIG_PREFIX}{key}"
        txn.execute(
            """
            INSERT INTO config_store (key, value, source, is_secret, updated_at)
            SELECT %s, value, source, is_secret, updated_at
              FROM config_store
             WHERE key = %s
               AND NOT EXISTS (SELECT 1 FROM config_store WHERE key = %s)
            """,
            (falkordb_key, stale_key, falkordb_key),
        )


def _delete_neo4j_config_rows(txn: Transaction) -> None:
    txn.execute(
        "DELETE FROM config_store WHERE key LIKE %s",
        (f"{_LEGACY_NEO4J_CONFIG_PREFIX}%",),
    )


def _delete_orphaned_legacy_auth_secret(txn: Transaction) -> None:
    txn.execute(
        """
        DELETE FROM secrets
         WHERE name = %s
           AND NOT EXISTS (SELECT 1 FROM config_store WHERE value = %s)
        """,
        (_LEGACY_AUTH_SECRET_NAME, _LEGACY_AUTH_SECRET_REF),
    )
