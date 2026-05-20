from __future__ import annotations

import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_migrate_sqlite_to_postgres_runs_reseed_after_copy_before_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration = importlib.import_module("gobby.storage.migration.sqlite_to_postgres")
    source = tmp_path / "gobby-hub.db"
    source.write_bytes(b"sqlite fixture")
    events: list[str] = []

    monkeypatch.setattr(
        migration,
        "_assert_source_schema_supported",
        lambda *_args, **_kwargs: events.append("schema"),
    )
    monkeypatch.setattr(
        migration,
        "_copy_sqlite_rows_to_postgres",
        lambda *_args, **_kwargs: events.append("copy"),
    )
    monkeypatch.setattr(
        migration,
        "reseed_identity_sequences",
        lambda *_args, **_kwargs: events.append("reseed"),
    )
    monkeypatch.setattr(
        migration,
        "validate_migration",
        lambda *_args, **_kwargs: events.append("validate"),
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
    assert result["rows"] >= 0
    assert result["tables"] >= 0


def test_migrate_sqlite_to_postgres_dry_run_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    migration = importlib.import_module("gobby.storage.migration.sqlite_to_postgres")
    source = tmp_path / "gobby-hub.db"
    source.write_bytes(b"sqlite fixture")
    forbidden: list[str] = []

    monkeypatch.setattr(migration, "_assert_source_schema_supported", lambda *_args: None)
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
