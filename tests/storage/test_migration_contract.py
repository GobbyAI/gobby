"""Contracts for the post-M0 flattened PostgreSQL migration baseline."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.migration_flatten import load_flatten_evidence
from gobby.storage.migrations import (
    BASELINE_VERSION,
    Migration,
    MigrationRunner,
    MigrationUnsupportedError,
    baseline_checksum,
    latest_known_version,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "src/gobby/storage/migrations"
BASELINE_PATH = REPO_ROOT / "src/gobby/storage/postgres_baseline_schema.sql"


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _AuditHub:
    dialect = "postgres"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.audit_query_count = 0

    @contextmanager
    def transaction(self) -> Iterator[_AuditHub]:
        yield self

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Result:
        del params
        if "SELECT version, filename, checksum" in sql:
            self.audit_query_count += 1
            return _Result(self._rows)
        raise AssertionError(f"unexpected audit query: {sql}")

    def close(self) -> None:
        return None


def _runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    applied: set[int],
    rows: list[dict[str, Any]],
    migrations: list[Migration] | None = None,
) -> MigrationRunner:
    runner = MigrationRunner(cast(HubDatabase, _AuditHub(rows)))
    monkeypatch.setattr(runner, "_ensure_schema_migrations_table", lambda: None)
    monkeypatch.setattr(runner, "_read_applied_versions", lambda: applied)
    monkeypatch.setattr(runner, "_discover_migrations", lambda: migrations or [])
    return runner


def test_post_m0_baseline_folds_every_existing_migration() -> None:
    assert BASELINE_VERSION == 375
    assert latest_known_version() == BASELINE_VERSION
    assert sorted(MIGRATIONS_DIR.glob("*.sql")) == []


def test_generated_baseline_matches_pinned_flatten_evidence() -> None:
    evidence = load_flatten_evidence(REPO_ROOT)

    assert evidence.baseline_version == BASELINE_VERSION
    assert evidence.baseline_checksum == baseline_checksum()
    assert evidence.applied_versions[-1] == BASELINE_VERSION
    assert tuple(receipt.version for receipt in evidence.receipts) == tuple(range(354, 376))


def test_generated_baseline_defines_attested_bookkeeping_columns() -> None:
    baseline = BASELINE_PATH.read_text(encoding="utf-8")

    assert "CREATE TABLE schema_migrations" in baseline
    assert "filename text" in baseline
    assert "checksum text" in baseline


def test_new_runner_accepts_singleton_flattened_baseline_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(
        monkeypatch,
        applied={375},
        rows=[
            {
                "version": 375,
                "filename": "baseline@375",
                "checksum": baseline_checksum(),
            }
        ],
    )

    runner._audit_migration_state()

    assert cast(_AuditHub, runner._hub).audit_query_count == 1


def test_new_runner_rejects_unflattened_attested_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(
        monkeypatch,
        applied={354, 375},
        rows=[
            {
                "version": 354,
                "filename": "354_migration_bookkeeping.sql",
                "checksum": "a" * 64,
            },
            {
                "version": 375,
                "filename": "375_machine_scope.sql",
                "checksum": "b" * 64,
            },
        ],
    )

    with pytest.raises(
        MigrationUnsupportedError,
        match=r"Applied migration v354 has no matching on-disk file",
    ):
        runner._audit_migration_state()


def test_old_runner_rejects_new_baseline_receipt_as_filename_skew(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.storage.migrations as module

    old_migration_path = tmp_path / "375_machine_scope.sql"
    old_migration_path.write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(module, "BASELINE_VERSION", 305)
    runner = _runner(
        monkeypatch,
        applied=set(range(305, 376)),
        rows=[
            {
                "version": 375,
                "filename": "baseline@375",
                "checksum": baseline_checksum(),
            }
        ],
        migrations=[Migration(375, "machine_scope", old_migration_path)],
    )

    with pytest.raises(MigrationUnsupportedError, match=r"filename mismatch for v375"):
        runner._audit_migration_state()
