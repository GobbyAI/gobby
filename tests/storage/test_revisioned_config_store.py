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
from pydantic import ValidationError

from gobby.config.embedding_keys import AI_EMBEDDING_API_KEY_KEY, EMBEDDING_API_KEY_SECRET_NAME
from gobby.config.values import ConfigValuesError
from gobby.storage.config_mutations import (
    MAX_CONFIG_REVISION,
    ConfigConflictError,
    ConfigMutations,
    ConfigPatch,
    ConfigRevisionExhaustedError,
    ConfigValidationError,
    SecretUpdate,
    config_key_to_secret_name,
    embedding_mutation_context,
)
from gobby.storage.config_repository import (
    ConfigRepository,
    UnknownStoredConfigKeyError,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import HubDatabase, Row, Transaction
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.mcp_secrets import MCPSecretSlot, cleanup_replaced_mcp_secrets
from gobby.storage.projects import LocalProjectManager
from gobby.storage.schema_contract import apply_schema
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.integration


def test_config_values_error_survives_database_transaction_context(
    revision_db: HubDatabase,
) -> None:
    error = ConfigValuesError(
        code="invalid_value",
        message="invalid configuration value",
        path=("values", "ui.enabled"),
        status_code=422,
    )

    with pytest.raises(ConfigValuesError) as raised:
        with revision_db.transaction():
            raise error

    assert raised.value is error
    assert raised.value.public_body() == {
        "error": {
            "code": "invalid_value",
            "message": "invalid configuration value",
            "path": ["values", "ui.enabled"],
            "retryable": False,
        }
    }


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
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    scoped_url = make_conninfo(database_url, dbname=database_name)
    with psycopg.connect(scoped_url, autocommit=True) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
    # Baseline alone lacks migration-owned shapes (e.g. the 391 creation-time
    # defaults); apply the full identity-enforced chain like production does.
    apply_schema(scoped_url)
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


def test_type_adapter_internal_type_error_propagates_and_is_logged(
    mutations: ConfigMutations,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_adapter(*_args: object, **_kwargs: object) -> object:
        raise TypeError("adapter implementation failed")

    monkeypatch.setattr(
        "gobby.storage.config_mutations.TypeAdapter.validate_json",
        fail_adapter,
    )

    with pytest.raises(TypeError, match="adapter implementation failed"):
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(values={"ui.enabled": True}),
        )

    assert "Configuration type adapter failed for ui.enabled" in caplog.text


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
        def read_rows(self, transaction: Transaction) -> list[Row]:
            revision_read.set()
            assert writer_committed.wait(timeout=5)
            return super().read_rows(transaction)

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
    patch = ConfigPatch(values={"auth.api_token_hash": "hash"})

    with pytest.raises(ConfigValidationError, match="restricted"):
        mutations.patch(expected_revision=0, patch=patch)

    result = mutations.patch_internal(expected_revision=0, patch=patch, source="test")
    assert result.revision == 1


def test_colliding_secret_key_names_stay_distinct(
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    first = "ai.generation.endpoints.alpha.api_key"
    second = "ai.generation.endpoints.beta.api_key"
    result = mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "ai.generation.endpoints.alpha.api_base": "https://alpha.example/v1",
                "ai.generation.endpoints.alpha.model": "model-a",
                "ai.generation.endpoints.beta.api_base": "https://beta.example/v1",
                "ai.generation.endpoints.beta.model": "model-b",
            },
            secrets={
                first: SecretUpdate("alpha-secret"),
                second: SecretUpdate("beta-secret"),
            },
        ),
    )

    first_name = config_key_to_secret_name(first)
    second_name = config_key_to_secret_name(second)
    assert first_name != second_name
    assert secret_store.get(first_name) == "alpha-secret"
    assert secret_store.get(second_name) == "beta-secret"

    mutations.patch(
        expected_revision=result.revision,
        patch=ConfigPatch(
            unset=frozenset(
                {
                    first,
                    "ai.generation.endpoints.alpha.api_base",
                    "ai.generation.endpoints.alpha.model",
                }
            )
        ),
    )

    assert secret_store.get(first_name) is None
    assert secret_store.get(second_name) == "beta-secret"


def test_derived_secret_name_is_bounded_with_hash_tail() -> None:
    first_key = f"ai.generation.endpoints.{'x' * 500}.api_key"
    second_key = f"ai.generation.endpoints.{'x' * 500}.different_api_key"

    first_name = config_key_to_secret_name(first_key)
    second_name = config_key_to_secret_name(second_key)

    assert first_name != second_name
    assert len(first_name) == 200
    assert len(first_name.rsplit("_", 1)[-1]) == 8
    assert len(second_name) == 200
    assert len(second_name.rsplit("_", 1)[-1]) == 8


def test_shared_secret_reference_survives_partial_unset(
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    first = "ai.generation.endpoints.alpha.api_key"
    second = "ai.generation.endpoints.beta.api_key"
    result = mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "ai.generation.endpoints.alpha.api_base": "https://alpha.example/v1",
                "ai.generation.endpoints.alpha.model": "model-a",
                "ai.generation.endpoints.beta.api_base": "https://beta.example/v1",
                "ai.generation.endpoints.beta.model": "model-b",
            },
            secrets={
                first: SecretUpdate("shared-secret", name="shared_endpoint_key"),
                second: SecretUpdate("shared-secret", name="shared_endpoint_key"),
            },
        ),
    )

    partial = mutations.patch(
        expected_revision=result.revision,
        patch=ConfigPatch(unset=frozenset({first})),
    )
    assert secret_store.get("shared_endpoint_key") == "shared-secret"

    mutations.patch(
        expected_revision=partial.revision,
        patch=ConfigPatch(unset=frozenset({second})),
    )
    assert secret_store.get("shared_endpoint_key") is None


def test_shared_secret_reference_survives_endpoint_rename(
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    alpha = "ai.generation.endpoints.alpha"
    beta = "ai.generation.endpoints.beta"
    seeded = mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                f"{alpha}.api_base": "https://alpha.example/v1",
                f"{alpha}.model": "model-a",
            },
            secrets={
                f"{alpha}.api_key": SecretUpdate(
                    "shared-secret",
                    name="shared_endpoint_key",
                )
            },
        ),
    )

    mutations.patch(
        expected_revision=seeded.revision,
        patch=ConfigPatch(
            values={
                f"{beta}.api_base": "https://beta.example/v1",
                f"{beta}.model": "model-b",
                f"{beta}.api_key": "$secret:shared_endpoint_key",
            },
            unset=frozenset(
                {
                    f"{alpha}.api_base",
                    f"{alpha}.model",
                    f"{alpha}.api_key",
                }
            ),
        ),
    )

    assert secret_store.get("shared_endpoint_key") == "shared-secret"
    binding = mutations.repository.read().secret_bindings[f"{beta}.api_key"]
    assert binding.plaintext == "shared-secret"


def test_secret_references_survive_two_key_swap(
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    alpha = "ai.generation.endpoints.alpha"
    beta = "ai.generation.endpoints.beta"
    seeded = mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                f"{alpha}.api_base": "https://alpha.example/v1",
                f"{alpha}.model": "model-a",
                f"{beta}.api_base": "https://beta.example/v1",
                f"{beta}.model": "model-b",
            },
            secrets={
                f"{alpha}.api_key": SecretUpdate("alpha-secret", name="alpha_endpoint_key"),
                f"{beta}.api_key": SecretUpdate("beta-secret", name="beta_endpoint_key"),
            },
        ),
    )

    mutations.patch(
        expected_revision=seeded.revision,
        patch=ConfigPatch(
            values={
                f"{alpha}.api_key": "$secret:beta_endpoint_key",
                f"{beta}.api_key": "$secret:alpha_endpoint_key",
            }
        ),
    )

    assert secret_store.get("alpha_endpoint_key") == "alpha-secret"
    assert secret_store.get("beta_endpoint_key") == "beta-secret"
    bindings = mutations.repository.read().secret_bindings
    assert bindings[f"{alpha}.api_key"].plaintext == "beta-secret"
    assert bindings[f"{beta}.api_key"].plaintext == "alpha-secret"


def test_secret_reference_in_ordinary_string_value_prevents_deletion(
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    key = "ai.generation.endpoints.alpha.api_key"
    seeded = mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "ai.generation.endpoints.alpha.api_base": "https://alpha.example/v1",
                "ai.generation.endpoints.alpha.model": "model-a",
                "telemetry.service_name": "$secret:shared_endpoint_key",
            },
            secrets={key: SecretUpdate("shared-secret", name="shared_endpoint_key")},
        ),
    )

    mutations.patch(
        expected_revision=seeded.revision,
        patch=ConfigPatch(unset=frozenset({key})),
    )

    assert secret_store.get("shared_endpoint_key") == "shared-secret"


def test_named_secret_delete_normalizes_and_protects_composite_references(
    revision_db: HubDatabase,
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    name = "Case_Shared_Secret"
    key = "ai.generation.endpoints.alpha.api_key"
    mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "ai.generation.endpoints.alpha.api_base": "https://alpha.example/v1",
                "ai.generation.endpoints.alpha.model": "model-a",
                "telemetry.service_name": f"prefix $secret:{name.lower()} suffix",
            },
            secrets={key: SecretUpdate("shared-secret", name=name)},
        ),
    )

    deleted = ConfigStore(revision_db).delete_named_secret(secret_store, name.upper())

    assert deleted is False
    assert secret_store.get(name) == "shared-secret"


def test_mcp_holder_prevents_named_secret_deletion(
    revision_db: HubDatabase,
    secret_store: SecretStore,
) -> None:
    project = LocalProjectManager(revision_db).create(name="secret-holder")
    name = "mcp_shared_secret"
    secret_store.set(name=name, plaintext_value="shared-secret")
    LocalMCPManager(revision_db).upsert(
        name="secret-holder-server",
        transport="stdio",
        project_id=project.id,
        command="server",
        env={"TOKEN": f"prefix $secret:{name.upper()} suffix"},
    )

    deleted = ConfigStore(revision_db).delete_named_secret(secret_store, name.upper())

    assert deleted is False
    assert secret_store.get(name) == "shared-secret"


def test_mcp_cleanup_preserves_secret_held_by_config(
    revision_db: HubDatabase,
    secret_store: SecretStore,
) -> None:
    slot = MCPSecretSlot("global", "shared", "server", "env", "TOKEN")
    reference = f"$secret:{slot.name}"
    secret_store.set(
        name=slot.name,
        plaintext_value="shared-secret",
        category="mcp_server",
        description=slot.description,
    )
    ConfigMutations(revision_db, secret_store=secret_store).patch(
        expected_revision=0,
        patch=ConfigPatch(values={"telemetry.service_name": f"config holds {reference}"}),
    )

    cleanup_replaced_mcp_secrets(
        secret_store,
        persistence=slot.persistence,
        scope=slot.scope,
        server_name=slot.server_name,
        old_env={slot.key: reference},
        old_headers=None,
        new_env={},
        new_headers=None,
    )

    assert secret_store.get(slot.name) == "shared-secret"


def test_namespace_replacement_preserves_managed_embedding_keys(
    mutations: ConfigMutations,
) -> None:
    seeded = mutations.patch_internal(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "ai.embeddings.model": "text-embedding-3-small",
                "ai.embeddings.dim": 1536,
                "ai.embeddings.api_base": "https://api.openai.example/v1",
            }
        ),
        source="install",
    )

    replaced = mutations.replace_namespace(
        namespace="daemon",
        expected_revision=seeded.revision,
        patch=ConfigPatch(values={"websocket.ping_interval": 22}),
    )

    snapshot = mutations.repository.read(resolve_secrets=False)
    assert replaced.changed_keys == frozenset({"websocket.ping_interval"})
    assert snapshot.overrides["ai.embeddings.model"] == "text-embedding-3-small"
    assert snapshot.overrides["ai.embeddings.dim"] == 1536
    assert snapshot.overrides["websocket.ping_interval"] == 22


def test_decision_snapshot_conflicts_are_detected(mutations: ConfigMutations) -> None:
    mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(values={"ui.enabled": True}),
    )
    stale = mutations.repository.read(resolve_secrets=False)
    mutations.patch(
        expected_revision=stale.revision,
        patch=ConfigPatch(values={"rules.enforcement_enabled": False}),
    )

    with pytest.raises(ConfigConflictError):
        mutations.patch(
            expected_revision=stale.revision,
            patch=ConfigPatch(unset=frozenset({"ui.enabled"})),
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("cors_origins", ("https://example.invalid",)),
        ("ui.enabled", object()),
    ],
)
def test_config_mutations_reject_python_only_values(
    mutations: ConfigMutations,
    key: str,
    value: object,
) -> None:
    revision = mutations.repository.current_revision()
    with pytest.raises(ConfigValidationError, match="Invalid value"):
        mutations.patch(
            expected_revision=revision,
            patch=ConfigPatch(values={key: value}),
        )
    assert mutations.repository.current_revision() == revision


def test_ambient_read_is_coherent(revision_db: HubDatabase) -> None:
    repository = ConfigRepository(revision_db)
    mutations = ConfigMutations(revision_db)

    with embedding_mutation_context(revision_db):
        empty = repository.read(resolve_secrets=False)
    assert empty.revision == 0
    assert dict(empty.overrides) == {}

    mutations.patch(
        expected_revision=empty.revision,
        patch=ConfigPatch(values={"ui.enabled": True}),
    )
    with embedding_mutation_context(revision_db):
        populated = repository.read(resolve_secrets=False)
    assert populated.revision == 1
    assert populated.overrides["ui.enabled"] is True
    assert populated.row_revisions == {"ui.enabled": 1}


def test_voice_binding_plaintext_api_key_is_rejected_at_storage(
    revision_db: HubDatabase,
    mutations: ConfigMutations,
) -> None:
    """Every writer hits the mutations guard: plaintext audio keys never persist."""
    binding = {
        "provider": "speaches",
        "url": "http://localhost:8080/v1",
        "model": "whisper-large-v3",
        "api_key": "raw-plaintext-key",
    }

    with pytest.raises(ConfigValidationError, match=r"\$secret:NAME"):
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(values={"voice.openai_compatible_audio": [binding]}),
        )

    assert revision_db.fetchall("SELECT key FROM config_store") == []

    reference_binding = dict(binding, api_key="$secret:SPEACHES_KEY")
    result = mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(values={"voice.openai_compatible_audio": [reference_binding]}),
    )

    assert result.revision == 1
    assert result.changed_keys == frozenset({"voice.openai_compatible_audio"})


def test_runtime_candidate_resolves_secret_references(
    revision_db: HubDatabase,
    mutations: ConfigMutations,
    secret_store: SecretStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime consumers get plaintext for reference-secrecy keys (#20032).

    Stored rows and snapshot values keep the ``$secret:`` reference form;
    only the materialized runtime DaemonConfig carries plaintext, otherwise
    FalkorDB, Qdrant, and generation-endpoint clients authenticate with the
    reference literal.
    """
    key = "databases.falkordb.password"
    mutations.patch(
        expected_revision=0,
        patch=ConfigPatch(secrets={key: SecretUpdate("falkor-plaintext-pw")}),
    )

    repository = ConfigRepository(revision_db, secret_store=secret_store)
    snapshot = repository.read(resolve_secrets=True)

    def reject_out_of_snapshot_read(_name: str) -> str | None:
        raise AssertionError("runtime candidate must use the captured secret bindings")

    monkeypatch.setattr(secret_store, "get", reject_out_of_snapshot_read)
    candidate = repository.runtime_candidate(dict(snapshot.overrides), snapshot.secret_bindings)

    assert snapshot.values[key] == f"$secret:{config_key_to_secret_name(key)}"
    assert candidate.databases.falkordb.password == "falkor-plaintext-pw"


def test_invalid_secret_candidate_error_does_not_contain_plaintext(
    revision_db: HubDatabase,
    mutations: ConfigMutations,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = "databases.falkordb.password"
    invalid = "plaintext must not leak"

    with pytest.raises(ConfigValidationError) as captured:
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(secrets={key: SecretUpdate(invalid)}),
        )

    assert captured.value.key == key
    assert str(captured.value) == "Secret configuration value is invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert invalid not in str(captured.value)
    assert key in caplog.text
    assert invalid not in caplog.text


def test_candidate_validation_attributes_nonsecret_failure_with_secret_patch(
    mutations: ConfigMutations,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ValidationError.from_exception_data(
        "DaemonConfig",
        [
            {
                "type": "greater_than",
                "loc": ("websocket", "ping_interval"),
                "input": 0,
                "ctx": {"gt": 0},
            }
        ],
    )

    def invalid_candidate(*_args: object) -> None:
        raise error

    monkeypatch.setattr(mutations.repository, "runtime_candidate", invalid_candidate)

    with pytest.raises(ConfigValidationError) as captured:
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(
                values={"websocket.ping_interval": 10},
                secrets={"databases.falkordb.password": SecretUpdate("Valid-Password-123")},
            ),
        )

    assert captured.value.key == "websocket.ping_interval"
    assert "greater than 0" in str(captured.value)


def test_reference_repoint_validates_resolved_plaintext(
    mutations: ConfigMutations,
    secret_store: SecretStore,
) -> None:
    key = "databases.falkordb.password"
    secret_store.set(name="invalid_falkor", plaintext_value="invalid password")

    with pytest.raises(ConfigValidationError) as captured:
        mutations.patch(
            expected_revision=0,
            patch=ConfigPatch(values={key: "$secret:invalid_falkor"}),
        )

    assert captured.value.key == key
    assert str(captured.value) == "Secret configuration value is invalid"


def test_unbound_canonical_named_secret_is_validated(
    revision_db: HubDatabase,
    secret_store: SecretStore,
) -> None:
    name = config_key_to_secret_name("databases.falkordb.password")

    with pytest.raises(ConfigValidationError):
        ConfigStore(revision_db).set_named_secret(
            secret_store,
            name,
            "invalid password",
            category="general",
            description=None,
        )

    assert secret_store.get(name) is None


def test_expected_revision_domain_is_rejected(mutations: ConfigMutations) -> None:
    """Storage rejects out-of-domain expected revisions before touching rows."""
    from typing import Any, cast

    patch = ConfigPatch(values={"rules.enforcement_enabled": True})
    for invalid in (-1, MAX_CONFIG_REVISION + 1, True, "0", 1.5, None):
        with pytest.raises(ConfigValidationError, match="expected_revision"):
            mutations.patch(expected_revision=cast(Any, invalid), patch=patch)
