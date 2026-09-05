"""Real PostgreSQL recovery of exactly inventoried wiki settings and discovery."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from gobby.storage.config_mutations import CONFIG_CHANGED_CHANNEL
from gobby.storage.config_repository import (
    MAX_CONFIG_REVISION,
    ConfigRepository,
    UnknownStoredConfigKeyError,
)
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.secrets import SecretStore
from scripts.wiki_retirement_config import WIKI_TOOLS, owned_config_key
from scripts.wiki_retirement_inventory import RetirementError, StoreTarget, canonical, sha
from scripts.wiki_retirement_storage import Datastores

pytestmark = pytest.mark.integration


@pytest.fixture
def stores(postgres_db: PostgresHubDatabase) -> Iterator[Datastores]:
    row = postgres_db.fetchone("SELECT current_schema() AS name")
    assert row is not None
    value = Datastores(
        postgres_db.conninfo,
        "http://127.0.0.1:6338",
        "redis://127.0.0.1:16389",
        schema=row["name"],
        isolated=True,
    )
    try:
        yield value
    finally:
        value.close()


def seed_config(db: PostgresHubDatabase) -> None:
    for key, value, secret in (
        ("wiki.enabled", "false", False),
        ("wiki.codewiki_nightly_enabled", "false", False),
        ("wiki.codewiki_project_scopes_by_name.gobby", '["src", "crates"]', False),
        ("ui.enabled", "true", False),
        ("databases.falkordb.password", '"$secret:preserved"', True),
    ):
        db.execute(
            "INSERT INTO config_store(key,value,source,is_secret,revision) VALUES (%s,%s,%s,%s,%s)",
            (key, value, "user", secret, 0),
        )


def target(stores: Datastores, kind: str) -> StoreTarget:
    return next(item for item in stores.configuration.inventory("fixture") if item.kind == kind)


def test_config_retirement_roundtrip_preserves_unrelated_settings_and_notifies(
    postgres_db: PostgresHubDatabase,
    stores: Datastores,
    tmp_path: Path,
    mock_machine_id: str,
) -> None:
    seed_config(postgres_db)
    secrets = SecretStore(postgres_db, gobby_home=tmp_path)
    secrets.set("preserved", "isolated-test-value")
    secret_rows = postgres_db.fetchall("SELECT * FROM secrets ORDER BY id")
    repository = ConfigRepository(postgres_db)
    with pytest.raises(UnknownStoredConfigKeyError, match="wiki.codewiki_nightly_enabled"):
        repository.read_bounded(resolve_secrets=False)
    survivor_query = "SELECT * FROM config_store WHERE NOT starts_with(key,'wiki.') ORDER BY key"
    survivors = postgres_db.fetchall(survivor_query)
    item = target(stores, "config")
    assert item.count == 3
    data = stores.backup(item)
    with psycopg.connect(postgres_db.conninfo, autocommit=True) as listener:
        listener.execute(f"LISTEN {CONFIG_CHANGED_CHANNEL}")
        stores.delete(item)
        notices = list(listener.notifies(timeout=1, stop_after=1))
    assert [(notice.channel, notice.payload) for notice in notices] == [
        (CONFIG_CHANGED_CHANNEL, "1")
    ]
    assert stores.capture(item) is None
    snapshot = repository.read_bounded(resolve_secrets=False)
    assert snapshot.revision == 1
    assert snapshot.overrides["ui.enabled"] is True
    assert snapshot.secret_bindings["databases.falkordb.password"].reference == "$secret:preserved"
    assert postgres_db.fetchall(survivor_query) == survivors
    stores.restore(item, data)
    assert stores.capture(item) == data
    assert repository.current_revision() == 2
    stores.restore(item, data)
    assert repository.current_revision() == 2
    assert postgres_db.fetchall(survivor_query) == survivors
    stores.delete(item)
    assert repository.read_bounded(resolve_secrets=False).revision == 3
    assert postgres_db.fetchall("SELECT * FROM secrets ORDER BY id") == secret_rows
    assert secrets.get("preserved") == "isolated-test-value"


def test_config_drift_and_failed_transaction_leave_all_rows_intact(
    postgres_db: PostgresHubDatabase,
    stores: Datastores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_config(postgres_db)
    item = target(stores, "config")
    postgres_db.execute("UPDATE config_store SET value='true' WHERE key='wiki.enabled'")
    with pytest.raises(RetirementError, match="changed before locked deletion"):
        stores.delete(item)
    current = target(stores, "config")
    before = postgres_db.fetchall("SELECT * FROM config_store ORDER BY key")

    def interrupt(*args: object) -> None:
        raise InterruptedError("simulated interruption after row deletion")

    with monkeypatch.context() as patch:
        patch.setattr(stores.configuration, "_advance", interrupt)
        with pytest.raises(InterruptedError, match="simulated interruption"):
            stores.delete(current)
    assert postgres_db.fetchall("SELECT * FROM config_store ORDER BY key") == before
    assert ConfigRepository(postgres_db).current_revision() == 0
    stores.delete(current)
    assert stores.capture(current) is None
    assert ConfigRepository(postgres_db).current_revision() == 1


@pytest.mark.parametrize(
    "key", ["wiki", "wiki.unknown", "wiki.codewiki_project_scopes_by_name.raw.dot"]
)
def test_unknown_wiki_keys_fail_inventory_without_deletion(
    postgres_db: PostgresHubDatabase,
    stores: Datastores,
    key: str,
) -> None:
    postgres_db.execute("INSERT INTO config_store(key,value) VALUES (%s,%s)", (key, "false"))
    with pytest.raises(RetirementError, match="legacy wiki configuration key"):
        stores.configuration.inventory("fixture")
    assert postgres_db.fetchone("SELECT key FROM config_store WHERE key=%s", (key,)) == {"key": key}


def test_key_ownership_requires_canonical_dynamic_segment() -> None:
    assert owned_config_key("wiki.codewiki_project_scopes_by_name.project%2Ename")
    assert not owned_config_key("ai.generation.profile_defaults.feature_low")
    assert not owned_config_key("other_wiki.enabled")


def test_discovery_roundtrip_preserves_other_servers_and_exact_ids(
    postgres_db: PostgresHubDatabase,
    stores: Datastores,
) -> None:
    project = LocalProjectManager(postgres_db).create("retirement-test")
    for name in sorted(WIKI_TOOLS):
        postgres_db.execute(
            "INSERT INTO tool_schema_hashes(server_name,tool_name,schema_hash,project_id) VALUES (%s,%s,%s,%s)",
            ("gobby-wiki", name, "legacy-hash", project.id),
        )
    postgres_db.execute(
        "INSERT INTO tool_schema_hashes(server_name,tool_name,schema_hash,project_id) VALUES (%s,%s,%s,%s)",
        ("gobby-code", "wiki_search", "preserve-other-server", project.id),
    )
    survivor = postgres_db.fetchone(
        "SELECT * FROM tool_schema_hashes WHERE server_name='gobby-code'"
    )
    item = target(stores, "discovery")
    assert item.count == 14
    data = stores.backup(item)
    stores.delete(item)
    assert stores.capture(item) is None
    stores.restore(item, data)
    assert stores.capture(item) == data
    stores.restore(item, data)
    assert stores.capture(item) == data
    assert (
        postgres_db.fetchone("SELECT * FROM tool_schema_hashes WHERE server_name='gobby-code'")
        == survivor
    )
    next_id = postgres_db.execute(
        "INSERT INTO tool_schema_hashes(server_name,tool_name,schema_hash,project_id) VALUES (%s,%s,%s,%s) RETURNING id",
        ("gobby-code", "search", "new-survivor", project.id),
    ).fetchone()
    assert next_id is not None
    assert next_id["id"] > 15


def test_discovery_drift_and_unknown_tools_are_refused(
    postgres_db: PostgresHubDatabase,
    stores: Datastores,
) -> None:
    project = LocalProjectManager(postgres_db).create("retirement-drift")
    postgres_db.execute(
        "INSERT INTO tool_schema_hashes(server_name,tool_name,schema_hash,project_id) VALUES (%s,%s,%s,%s)",
        ("gobby-wiki", "wiki_search", "old", project.id),
    )
    item = target(stores, "discovery")
    postgres_db.execute("UPDATE tool_schema_hashes SET schema_hash='changed'")
    with pytest.raises(RetirementError, match="changed before locked deletion"):
        stores.delete(item)
    postgres_db.execute("UPDATE tool_schema_hashes SET tool_name='unowned_tool'")
    with pytest.raises(RetirementError, match="exact legacy tool ownership"):
        stores.configuration.inventory("fixture")
    assert postgres_db.fetchone("SELECT COUNT(*) AS count FROM tool_schema_hashes") == {"count": 1}


def test_config_restore_refuses_changed_or_unowned_recovery(
    postgres_db: PostgresHubDatabase,
    stores: Datastores,
) -> None:
    seed_config(postgres_db)
    item = target(stores, "config")
    data = stores.backup(item)
    stores.delete(item)
    with pytest.raises(RetirementError, match="differs from inventory"):
        stores.restore(item, b"[]")
    unrelated = canonical([{"key": "ui.enabled", "is_secret": False}])
    with pytest.raises(RetirementError, match="exact wiki ownership"):
        stores.restore(item.model_copy(update={"digest": sha(unrelated)}), unrelated)
    assert stores.capture(item) is None
    stores.restore(item, data)
    assert stores.capture(item) == data


def test_revision_exhaustion_rolls_back_config_deletion(
    postgres_db: PostgresHubDatabase,
    stores: Datastores,
) -> None:
    seed_config(postgres_db)
    item = target(stores, "config")
    data = stores.backup(item)
    postgres_db.execute("UPDATE config_state SET revision=%s", (MAX_CONFIG_REVISION,))
    with pytest.raises(RetirementError, match="revision exhausted"):
        stores.delete(item)
    assert stores.capture(item) == data
    assert ConfigRepository(postgres_db).current_revision() == MAX_CONFIG_REVISION


def test_secret_marked_wiki_key_is_not_assumed_owned(
    postgres_db: PostgresHubDatabase,
    stores: Datastores,
) -> None:
    postgres_db.execute(
        "INSERT INTO config_store(key,value,is_secret) VALUES (%s,%s,%s)",
        ("wiki.enabled", '"$secret:unexpected"', True),
    )
    with pytest.raises(RetirementError, match="exact wiki ownership"):
        stores.configuration.inventory("fixture")
    assert postgres_db.fetchone("SELECT value FROM config_store WHERE key='wiki.enabled'") == {
        "value": '"$secret:unexpected"'
    }


def test_discovery_restore_prepares_only_missing_project_identity(
    postgres_db: PostgresHubDatabase,
    stores: Datastores,
) -> None:
    project = LocalProjectManager(postgres_db).create("missing-on-recovery")
    postgres_db.execute(
        "INSERT INTO tool_schema_hashes(server_name,tool_name,schema_hash,project_id) VALUES (%s,%s,%s,%s)",
        ("gobby-wiki", "wiki_search", "legacy", project.id),
    )
    item = target(stores, "discovery")
    data = stores.backup(item)
    stores.delete(item)
    postgres_db.execute("DELETE FROM projects WHERE id=%s", (project.id,))
    stores.restore(item, data)
    restored = LocalProjectManager(postgres_db).get(project.id)
    assert restored is not None
    assert restored.id == project.id
    assert stores.capture(item) == data
