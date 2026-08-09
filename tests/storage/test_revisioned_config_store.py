"""PostgreSQL acceptance tests for revisioned configuration storage."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from gobby.config.embedding_keys import AI_EMBEDDING_API_KEY_KEY, EMBEDDING_API_KEY_SECRET_NAME
from gobby.storage.config_mutations import (
    MAX_CONFIG_REVISION,
    ConfigConflictError,
    ConfigMutations,
    ConfigPatch,
    ConfigRevisionExhaustedError,
    ConfigValidationError,
    SecretUpdate,
    embedding_mutation_context,
)
from gobby.storage.config_repository import (
    ConfigRepository,
    UnknownStoredConfigKeyError,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import HubDatabase, Row, Transaction
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.integration


@pytest.fixture
def secret_store(
    revision_db: HubDatabase,
    tmp_path: Path,
    mock_machine_id: str,
) -> SecretStore:
    return SecretStore(revision_db, gobby_home=tmp_path)


@pytest.fixture
def mutations(revision_db: HubDatabase, secret_store: SecretStore) -> ConfigMutations:
    return ConfigMutations(revision_db, secret_store=secret_store)


@pytest.fixture
def revision_db() -> Iterator[PostgresHubDatabase]:
    database_url = os.environ["DATABASE_URL"]
    database_name = f"revisioned_config_{uuid.uuid4().hex}"
    baseline = Path("crates/gcore/assets/schema/baseline.sql").read_text()
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    scoped_url = make_conninfo(database_url, dbname=database_name)
    with psycopg.connect(scoped_url, autocommit=True) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
        connection.execute(baseline)
    database = PostgresHubDatabase(scoped_url)
    try:
        yield database
    finally:
        database.close()
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


@contextmanager
def _listener(revision_db: HubDatabase) -> Iterator[psycopg.Connection[object]]:
    assert isinstance(revision_db, PostgresHubDatabase)
    connection = psycopg.connect(revision_db.conninfo, autocommit=True)
    try:
        connection.execute("LISTEN gobby_config_changed")
        yield connection
    finally:
        connection.close()


def _notifications(connection: psycopg.Connection[object], timeout: float = 0.1) -> list[str]:
    return [notice.payload for notice in connection.notifies(timeout=timeout)]


def test_compare_and_swap_serializes_writers(mutations: ConfigMutations) -> None:
    ready = Barrier(3)

    def write(key: str) -> int | ConfigConflictError:
        ready.wait()
        try:
            return mutations.patch(
                expected_revision=0,
                patch=ConfigPatch(values={key: False if key.startswith("rules.") else True}),
            ).revision
        except ConfigConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write, "ui.enabled")
        second = executor.submit(write, "rules.enforcement_enabled")
        ready.wait()
        outcomes = (first.result(), second.result())

    assert sum(outcome == 1 for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, ConfigConflictError)]
    assert len(conflicts) == 1
    assert conflicts[0].expected_revision == 0
    assert conflicts[0].actual_revision == 1


def test_mutation_is_one_transaction(
    revision_db: HubDatabase,
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    seeded = mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(values={"rules.aggregate_blocks": False}),
    )
    assert seeded.revision == 1

    with _listener(revision_db) as listener:
        result = mutations.patch(
            expected_revision=1,
            patch=ConfigPatch(
                values={"ui.enabled": True},
                unset=frozenset({"rules.aggregate_blocks"}),
                secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("rotated-secret")},
            ),
        )
        notifications = _notifications(listener, timeout=1.0)

    rows = revision_db.fetchall(
        "SELECT key, value, is_secret, revision FROM config_store ORDER BY key"
    )
    revision = revision_db.fetchone("SELECT revision FROM config_state WHERE id = %s", (True,))

    assert result.revision == 2
    assert result.changed_keys == frozenset(
        {"ui.enabled", "rules.aggregate_blocks", AI_EMBEDDING_API_KEY_KEY}
    )
    assert [(row["key"], row["revision"]) for row in rows] == [
        (AI_EMBEDDING_API_KEY_KEY, 2),
        ("ui.enabled", 2),
    ]
    assert next(row for row in rows if row["key"] == AI_EMBEDDING_API_KEY_KEY)["is_secret"]
    assert revision == {"revision": 2}
    assert secret_store.get(EMBEDDING_API_KEY_SECRET_NAME) == "rotated-secret"
    assert notifications == ["2"]


def test_invalid_candidate_has_no_side_effects(
    revision_db: HubDatabase,
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    with _listener(revision_db) as listener:
        with pytest.raises(ConfigValidationError):
            mutations.patch(
                expected_revision=0,
                patch=ConfigPatch(
                    values={"hooks.adapter_timeout": 130.0},
                    secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("must-roll-back")},
                ),
            )
        notifications = _notifications(listener)

    assert revision_db.fetchall("SELECT key FROM config_store") == []
    assert revision_db.fetchone("SELECT revision FROM config_state WHERE id = %s", (True,)) == {
        "revision": 0
    }
    assert secret_store.get(EMBEDDING_API_KEY_SECRET_NAME) is None
    assert notifications == []


def test_effective_change_controls_revision(
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    default_value = mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(values={"tool_approvals.global_rules": []}),
    )
    changed = mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(values={"ui.enabled": True}),
    )
    same_value = mutations.patch(
        expected_revision=1,
        patch=ConfigPatch(values={"ui.enabled": True}),
    )
    first_secret = mutations.patch(
        expected_revision=1,
        patch=ConfigPatch(secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("one")}),
    )
    same_secret = mutations.patch(
        expected_revision=2,
        patch=ConfigPatch(secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("one")}),
    )
    rotated = mutations.patch(
        expected_revision=2,
        patch=ConfigPatch(secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate("two")}),
    )

    assert default_value.revision == 0
    assert default_value.changed_keys == frozenset()
    assert (changed.revision, same_value.revision) == (1, 1)
    assert same_value.changed_keys == frozenset()
    assert (first_secret.revision, same_secret.revision, rotated.revision) == (2, 2, 3)
    assert same_secret.changed_keys == frozenset()
    assert rotated.changed_keys == frozenset({AI_EMBEDDING_API_KEY_KEY})
    assert secret_store.get(EMBEDDING_API_KEY_SECRET_NAME) == "two"


def test_snapshot_read_is_repeatable_read_coherent(
    revision_db: HubDatabase,
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(values={"ui.enabled": True}),
    )
    revision_read = Event()
    writer_committed = Event()

    class PausingRepository(ConfigRepository):
        def _read_rows(self, transaction: Transaction) -> list[Row]:
            revision_read.set()
            assert writer_committed.wait(timeout=5)
            return super()._read_rows(transaction)

    repository = PausingRepository(revision_db, secret_store=secret_store)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(repository.read)
        assert revision_read.wait(timeout=5)
        writer = executor.submit(
            mutations.patch,
            expected_revision=1,
            patch=ConfigPatch(values={"rules.enforcement_enabled": False}),
        )
        assert writer.result(timeout=5).revision == 2
        writer_committed.set()
        snapshot = reader.result(timeout=5)

    assert snapshot.revision == 1
    assert snapshot.values["ui.enabled"] is True
    assert snapshot.values["rules.enforcement_enabled"] is True
    assert snapshot.row_revisions == {"ui.enabled": 1}


def test_startup_secrecy_repair_preserves_values_and_revision(revision_db: HubDatabase) -> None:
    revision_db.execute(
        """INSERT INTO config_store (key, value, source, is_secret, revision)
           VALUES (%s, %s, %s, %s, %s)""",
        ("ui.enabled", "true", "test", True, 0),
    )

    repaired = ConfigRepository(revision_db).reconcile_registry()

    assert repaired == frozenset({"ui.enabled"})
    assert revision_db.fetchone(
        "SELECT value, is_secret, revision FROM config_store WHERE key = %s",
        ("ui.enabled",),
    ) == {"value": "true", "is_secret": False, "revision": 0}
    assert revision_db.fetchone("SELECT revision FROM config_state WHERE id = %s", (True,)) == {
        "revision": 0
    }


def test_unknown_residual_row_fails_closed(revision_db: HubDatabase) -> None:
    revision_db.execute(
        """INSERT INTO config_store (key, value, source, is_secret, revision)
           VALUES (%s, %s, %s, %s, %s)""",
        ("removed.setting", "true", "test", False, 0),
    )

    with pytest.raises(UnknownStoredConfigKeyError, match="removed.setting"):
        ConfigRepository(revision_db).reconcile_registry()


def test_revision_ceiling_returns_exhausted(
    revision_db: HubDatabase,
    mutations: ConfigMutations,
) -> None:
    revision_db.execute(
        "UPDATE config_state SET revision = %s WHERE id = %s",
        (MAX_CONFIG_REVISION, True),
    )

    with _listener(revision_db) as listener:
        with pytest.raises(ConfigRevisionExhaustedError) as caught:
            mutations.patch(
                expected_revision=MAX_CONFIG_REVISION,
                patch=ConfigPatch(values={"ui.enabled": True}),
            )
        notifications = _notifications(listener)

    assert caught.value.code == "revision_exhausted"
    assert caught.value.retryable is False
    assert revision_db.fetchall("SELECT key FROM config_store") == []
    assert revision_db.fetchone("SELECT revision FROM config_state WHERE id = %s", (True,)) == {
        "revision": MAX_CONFIG_REVISION
    }
    assert notifications == []


def test_namespace_replacement_unsets_omitted_overrides(mutations: ConfigMutations) -> None:
    initial = mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "ui.enabled": True,
                "rules.enforcement_enabled": False,
            }
        ),
    )

    replaced = mutations.replace_namespace(
        namespace="ui",
        expected_revision=initial.revision,
        patch=ConfigPatch(values={}),
    )

    assert replaced.revision == 2
    assert replaced.changed_keys == frozenset({"ui.enabled"})


def test_restricted_keys_require_internal_mutation(mutations: ConfigMutations) -> None:
    patch = ConfigPatch(values={"auth.password_hash": "hash"})

    with pytest.raises(ConfigValidationError, match="restricted"):
        mutations.patch(expected_revision=0, patch=patch)

    result = mutations.patch_internal(expected_revision=0, patch=patch, source="test")
    assert result.revision == 1


def test_facade_mutation_reuses_ambient_transaction(revision_db: HubDatabase) -> None:
    store = ConfigStore(revision_db)
    store.set("ui.enabled", True)

    with embedding_mutation_context(revision_db):
        assert store.delete_all() == 1

    assert store.get_all() == {}
