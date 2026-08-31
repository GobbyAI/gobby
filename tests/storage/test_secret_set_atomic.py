"""Atomic PostgreSQL upsert tests for secret writes."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


def test_set_uses_one_upsert_and_returns_its_row() -> None:
    returned_at = datetime.now(UTC)
    db = MagicMock()
    db.fetchone.return_value = {
        "id": "returned-id",
        "name": "atomic_key",
        "category": "integration",
        "description": "returned description",
        "created_at": returned_at,
        "updated_at": returned_at,
        "project_id": GLOBAL_PROJECT_ID,
    }
    store = SecretStore(db)
    store._fernet = Fernet(Fernet.generate_key())

    info = store.set(
        "ATOMIC_KEY",
        "plaintext-value",
        category="integration",
        description="returned description",
    )

    db.fetchone.assert_called_once()
    db.execute.assert_not_called()
    sql, params = db.fetchone.call_args.args
    assert "INSERT INTO secrets" in sql
    assert "ON CONFLICT (name, project_id) DO UPDATE" in sql
    assert "RETURNING id, name, category, description, created_at, updated_at, project_id" in sql
    assert "plaintext-value" not in params
    assert GLOBAL_PROJECT_ID in params
    assert info.id == "returned-id"
    assert info.name == "atomic_key"
    assert info.category == "integration"
    assert info.description == "returned description"
    assert info.created_at == returned_at
    assert info.updated_at == returned_at
    assert info.project_id == GLOBAL_PROJECT_ID
    assert info.scope == "global"


def test_concurrent_first_writes_both_succeed(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    SecretStore(temp_db).ensure_ready()
    assert temp_db.fetchone("SELECT 1 FROM secrets WHERE name = %s", ("race_key",)) is None

    original_fetchone = temp_db.fetchone
    old_lookup_barrier = threading.Barrier(2)

    def synchronize_old_check_then_act(
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> Any:
        row = original_fetchone(sql, params)
        if "SELECT id FROM secrets WHERE name" in sql:
            old_lookup_barrier.wait(timeout=5)
        return row

    monkeypatch.setattr(temp_db, "fetchone", synchronize_old_check_then_act)
    start = threading.Barrier(2)
    writes = [
        ("value-a", "general", "writer-a"),
        ("value-b", "integration", "writer-b"),
    ]

    def write_secret(write: tuple[str, str, str]) -> Any:
        value, category, description = write
        start.wait(timeout=5)
        return SecretStore(temp_db).set(
            "RACE_KEY",
            value,
            category=category,
            description=description,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write_secret, writes))

    assert results[0].id == results[1].id
    for result, (_value, category, description) in zip(results, writes, strict=True):
        assert result.name == "race_key"
        assert result.category == category
        assert result.description == description

    rows = temp_db.fetchall(
        "SELECT id, category, description FROM secrets WHERE name = %s",
        ("race_key",),
    )
    assert len(rows) == 1
    assert rows[0]["id"] == results[0].id
    assert (rows[0]["category"], rows[0]["description"]) in {
        ("general", "writer-a"),
        ("integration", "writer-b"),
    }
    assert SecretStore(temp_db).get("RACE_KEY") in {"value-a", "value-b"}
