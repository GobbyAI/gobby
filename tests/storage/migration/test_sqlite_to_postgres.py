from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.storage.migrations import BASELINE_VERSION

pytestmark = pytest.mark.unit


class _FakePostgres:
    @contextmanager
    def transaction(self) -> Iterator[_FakePostgres]:
        yield self


def test_migrate_sqlite_to_postgres_runs_reseed_after_copy_before_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration = importlib.import_module("gobby.storage.migration.sqlite_to_postgres")
    source = tmp_path / "gobby-hub.db"
    source.write_bytes(b"sqlite fixture")
    events: list[str] = []

    @contextmanager
    def _postgres_context(_target: str) -> Iterator[_FakePostgres]:
        yield _FakePostgres()

    monkeypatch.setattr(
        migration,
        "_assert_source_schema_supported",
        lambda *_args, **_kwargs: events.append("schema"),
    )
    monkeypatch.setattr(migration, "_connect_postgres", _postgres_context)
    monkeypatch.setattr(migration, "active_install_mode", lambda: "docker")
    monkeypatch.setattr(
        migration, "_run_target_read_only_preflight", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(migration, "_apply_postgres_schema", lambda *_args: None)
    monkeypatch.setattr(
        migration, "_assert_target_ready_for_import", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(migration, "_fail_if_import_complete_marker", lambda *_args: None)
    monkeypatch.setattr(
        migration, "_acquire_import_lock", lambda *_args, **_kwargs: events.append("lock")
    )
    monkeypatch.setattr(migration, "_reset_seed_bearing_tables", lambda *_args: None)
    monkeypatch.setattr(migration, "_drop_bm25_indexes", lambda *_args: None)
    monkeypatch.setattr(migration, "_recreate_bm25_indexes", lambda *_args: None)
    monkeypatch.setattr(migration, "_default_import_log_path", lambda: tmp_path / "import.log")
    monkeypatch.setattr(
        migration,
        "_copy_sqlite_rows_to_postgres",
        lambda *_args, **_kwargs: _record_copy(migration, events),
    )
    monkeypatch.setattr(
        migration,
        "reseed_identity_sequences",
        lambda *_args, **_kwargs: events.append("reseed"),
    )
    monkeypatch.setattr(
        migration,
        "validate_migration",
        lambda *_args, **_kwargs: _record_validation(events),
    )
    monkeypatch.setattr(
        migration,
        "_write_import_complete_marker",
        lambda *_args, **_kwargs: events.append("marker"),
    )

    result = migration.migrate_sqlite_to_postgres(
        source=source,
        target="postgresql://gobby:secret@example.com/gobby",
        batch_size=1000,
        dry_run=False,
    )

    assert events == ["schema", "copy", "reseed", "validate", "marker"]
    assert result["rows"] == 3
    assert result["tables"] == 2
    assert result["dry_run"] is False
    assert result["log_path"] == str(tmp_path / "import.log")
    assert result["validation_artifact"] is None


def test_migrate_sqlite_to_postgres_dry_run_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration = importlib.import_module("gobby.storage.migration.sqlite_to_postgres")
    source = tmp_path / "gobby-hub.db"
    source.write_bytes(b"sqlite fixture")
    forbidden: list[str] = []

    @contextmanager
    def _postgres_context(_target: str) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(migration, "_assert_source_schema_supported", lambda *_args: None)
    monkeypatch.setattr(migration, "_connect_postgres", _postgres_context)
    monkeypatch.setattr(migration, "active_install_mode", lambda: "docker")
    monkeypatch.setattr(
        migration, "_run_target_read_only_preflight", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        migration,
        "_run_table_mapping_preflight",
        lambda *_args, **_kwargs: {"tasks": 2},
    )
    monkeypatch.setattr(
        migration, "_apply_postgres_schema", lambda *_args: forbidden.append("apply")
    )
    monkeypatch.setattr(
        migration, "_reset_seed_bearing_tables", lambda *_args: forbidden.append("reset")
    )
    monkeypatch.setattr(migration, "_drop_bm25_indexes", lambda *_args: forbidden.append("drop"))
    monkeypatch.setattr(
        migration, "_recreate_bm25_indexes", lambda *_args: forbidden.append("recreate")
    )
    monkeypatch.setattr(
        migration, "_copy_sqlite_rows_to_postgres", lambda *_args: forbidden.append("copy")
    )
    monkeypatch.setattr(
        migration, "reseed_identity_sequences", lambda *_args: forbidden.append("reseed")
    )
    monkeypatch.setattr(
        migration, "validate_migration", lambda *_args: forbidden.append("validate")
    )
    monkeypatch.setattr(
        migration,
        "_write_import_complete_marker",
        lambda *_args: forbidden.append("marker"),
    )

    result = migration.migrate_sqlite_to_postgres(
        source=source,
        target="postgresql://gobby:secret@example.com/gobby",
        batch_size=1000,
        dry_run=True,
    )

    assert forbidden == []
    assert result["dry_run"] is True
    assert result["rows"] == 2
    assert result["tables"] == 1
    assert result["log_path"] is None
    assert result["validation_artifact"] is None


def test_seed_bearing_tables_follow_postgres_baseline() -> None:
    migration = importlib.import_module("gobby.storage.migration.sqlite_to_postgres")

    tables = set(migration._seed_bearing_tables())

    assert {"projects", "sessions", "task_stages_registry", "task_type_default_stages"} <= tables
    assert "schema_migrations" not in tables
    assert "gobby_migration_state" not in tables


def test_assert_source_schema_supported_rejects_same_version_schema_drift() -> None:
    migration = importlib.import_module("gobby.storage.migration.sqlite_to_postgres")
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    source.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    source.execute("INSERT INTO schema_migrations (version) VALUES (?)", (BASELINE_VERSION,))
    source.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")

    with pytest.raises(migration.SqliteToPostgresMigrationError, match="fingerprint"):
        migration._assert_source_schema_supported(source)


def _record_copy(migration: Any, events: list[str]) -> Any:
    events.append("copy")
    return migration._CopyResult(rows=3, tables=2)


def _record_validation(events: list[str]) -> SimpleNamespace:
    events.append("validate")
    return SimpleNamespace(artifact_path=None)
