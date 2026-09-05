"""Direct wiki deletion inventories ownership without copying discarded content."""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from scripts.wiki_retirement_inventory import GRAPH, PG_TABLES, RetirementError, canonical, sha
from scripts.wiki_retirement_storage import Datastores, record


@pytest.fixture
def stores(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Datastores, MagicMock, MagicMock, MagicMock]]:
    adapter = object.__new__(Datastores)
    pg, qdrant, redis = MagicMock(), MagicMock(), MagicMock()
    adapter.pg = pg
    adapter.qdrant = qdrant
    adapter.redis = redis
    adapter.schema = "public"
    adapter._identities = {"postgres": "pg-owned", "qdrant": "q-owned", "falkor": "f-owned"}
    monkeypatch.setattr(adapter, "identities", lambda: adapter._identities or {})
    adapter.configuration = MagicMock()
    yield adapter, pg, qdrant, redis


def test_direct_postgres_reads_only_exact_table_counts(
    stores: tuple[Datastores, MagicMock, MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, pg, _, _ = stores
    selected = set(PG_TABLES[:2])
    checked: list[str] = []

    def exists(name: str) -> bool:
        checked.append(name)
        return name in selected

    monkeypatch.setattr(adapter, "_table_exists", exists)
    monkeypatch.setattr(
        adapter, "_table_rows", MagicMock(side_effect=AssertionError("content read"))
    )
    pg.execute.return_value.fetchone.return_value = (7,)
    assert json.loads(adapter._pg_descriptor()) == dict.fromkeys(selected, 7)
    assert checked == list(PG_TABLES)
    assert pg.execute.call_count == 2
    for call in pg.execute.call_args_list:
        assert "SELECT COUNT(*) FROM" in call.args[0].as_string()
        assert "to_jsonb" not in call.args[0].as_string()
    pg.rollback.assert_called_once()


def test_direct_qdrant_verifies_all_points_by_filtered_count_without_scroll(
    stores: tuple[Datastores, MagicMock, MagicMock, MagicMock],
) -> None:
    adapter, _, qdrant, _ = stores
    qdrant.collection_exists.return_value = True
    qdrant.count.return_value.count = 9
    payload = json.loads(adapter._qdrant_descriptor("gwiki_topic_rust") or b"null")
    assert payload == {
        "name": "gwiki_topic_rust",
        "namespace": "gwiki",
        "scope_kind": "topic",
        "scope_id": "rust",
        "count": 9,
    }
    qdrant.scroll.assert_not_called()
    qdrant.get_collection.assert_not_called()
    assert qdrant.count.call_args_list[0].kwargs == {"exact": True}
    conditions = qdrant.count.call_args_list[1].kwargs["count_filter"].must
    assert {item.key: item.match.value for item in conditions} == {
        "namespace": "gwiki",
        "scope_kind": "topic",
        "scope_id": "rust",
    }


def test_direct_qdrant_refuses_mixed_ownership_and_unowned_names(
    stores: tuple[Datastores, MagicMock, MagicMock, MagicMock],
) -> None:
    adapter, _, qdrant, _ = stores
    qdrant.collection_exists.return_value = True
    qdrant.count.side_effect = [MagicMock(count=9), MagicMock(count=8)]
    with pytest.raises(RetirementError, match="unowned points"):
        adapter._qdrant_descriptor("gwiki_project_example")
    qdrant.reset_mock()
    with pytest.raises(RetirementError, match="outside wiki scope"):
        adapter._qdrant_descriptor("code_symbols_example")
    qdrant.collection_exists.assert_not_called()


def test_direct_empty_collection_requires_exact_scope_evidence(
    stores: tuple[Datastores, MagicMock, MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _, qdrant, _ = stores
    qdrant.collection_exists.return_value = True
    qdrant.count.return_value.count = 0
    scope = MagicMock(return_value=False)
    monkeypatch.setattr(adapter, "_wiki_scope_exists", scope)
    with pytest.raises(RetirementError, match="ownership evidence"):
        adapter._qdrant_descriptor("gwiki_topic_rust_async")
    scope.assert_called_once_with("topic", "rust_async")
    scope.return_value = True
    assert json.loads(adapter._qdrant_descriptor("gwiki_topic_rust_async") or b"null")["count"] == 0


def test_direct_falkor_admits_only_the_dedicated_graph_without_graph_content(
    stores: tuple[Datastores, MagicMock, MagicMock, MagicMock],
) -> None:
    adapter, _, _, redis = stores
    redis.execute_command.side_effect = [1, [b"gobby_code", GRAPH.encode()]]
    assert json.loads(adapter._falkor_descriptor() or b"null") == {"graph": GRAPH}
    assert [call.args for call in redis.execute_command.call_args_list] == [
        ("EXISTS", GRAPH),
        ("GRAPH.LIST",),
    ]
    redis.dump.assert_not_called()
    redis.execute_command.side_effect = [1, [b"gobby_code"]]
    with pytest.raises(RetirementError, match="not a Falkor graph"):
        adapter._falkor_descriptor()
    redis.execute_command.side_effect = [0]
    assert adapter._falkor_descriptor() is None


def test_direct_inventory_never_calls_full_content_capture(
    stores: tuple[Datastores, MagicMock, MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _, qdrant, _ = stores
    for method in ("_pg_snapshot", "_qdrant_snapshot", "_falkor_snapshot", "_wiki_scopes"):
        monkeypatch.setattr(adapter, method, MagicMock(side_effect=AssertionError("full capture")))
    monkeypatch.setattr(adapter, "_pg_descriptor", lambda: canonical({PG_TABLES[0]: 12}))
    monkeypatch.setattr(adapter, "_qdrant_descriptor", lambda name: canonical({"count": 3}))
    monkeypatch.setattr(adapter, "_falkor_descriptor", lambda: canonical({"graph": GRAPH}))
    monkeypatch.setattr(adapter, "_table_rows", lambda table: [])
    monkeypatch.setattr(adapter.configuration, "inventory", MagicMock(return_value=[]))
    qdrant.get_collections.return_value.collections = [
        MagicMock(name="owned"),
        MagicMock(name="unrelated"),
    ]
    qdrant.get_collections.return_value.collections[0].name = "gwiki_project_example"
    qdrant.get_collections.return_value.collections[1].name = "code_symbols_keep"
    targets = adapter.inventory(direct=True)
    assert [(target.kind, target.name, target.count) for target in targets] == [
        ("postgres", "wiki_schema", 12),
        ("qdrant", "gwiki_project_example", 3),
        ("falkor", GRAPH, 1),
    ]


@pytest.mark.parametrize("changed", ["identity", "descriptor"])
def test_direct_delete_rechecks_identity_and_descriptor_before_mutation(
    stores: tuple[Datastores, MagicMock, MagicMock, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    adapter, _, _, _ = stores
    data = canonical({"count": 3})
    target = record("qdrant", "gwiki_project_example", data, 3, "q-owned")
    monkeypatch.setattr(adapter, "capture_direct", lambda selected: data)
    deletion = MagicMock()
    monkeypatch.setattr(adapter, "delete", deletion)
    if changed == "identity":
        adapter._identities = {"qdrant": "wrong"}
    else:
        monkeypatch.setattr(adapter, "capture_direct", lambda selected: canonical({"count": 4}))
    with pytest.raises(RetirementError, match="changed"):
        adapter.delete_direct(target)
    deletion.assert_not_called()


def test_direct_delete_delegates_exact_target_and_completed_retry_is_noop(
    stores: tuple[Datastores, MagicMock, MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _, qdrant, _ = stores
    data = canonical({"count": 3})
    target = record("qdrant", "gwiki_project_example", data, 3, "q-owned")
    monkeypatch.setattr(adapter, "capture_direct", MagicMock(side_effect=[data, None]))
    adapter.delete_direct(target)
    adapter.delete_direct(target)
    qdrant.delete_collection.assert_called_once_with("gwiki_project_example")


def test_direct_registry_capture_preserves_exact_locked_deletion_contract(
    stores: tuple[Datastores, MagicMock, MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _, _, _ = stores
    snapshot = canonical({"row": {"id": "wiki-job", "name": "wiki-refresh"}, "runs": []})
    target = record("job", "wiki-job", snapshot, 1, "pg-owned")
    capture = MagicMock(return_value=snapshot)
    monkeypatch.setattr(adapter, "capture", capture)
    deletion = MagicMock()
    monkeypatch.setattr(adapter, "delete", deletion)
    adapter.delete_direct(target)
    capture.assert_called_once_with(target)
    deletion.assert_called_once_with(target)
    assert target.digest == sha(snapshot)


def test_direct_postgres_target_and_schema_absence_checks_are_exact(
    stores: tuple[Datastores, MagicMock, MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _, _, _ = stores
    target = record("postgres", "unrelated", b"{}", 0, "pg-owned")
    with pytest.raises(RetirementError, match="Unexpected PostgreSQL target"):
        adapter.capture_direct(target)
    monkeypatch.setattr(
        adapter, "_pg_snapshot", MagicMock(side_effect=AssertionError("full capture"))
    )
    monkeypatch.setattr(adapter, "_pg_descriptor", lambda: b"{}")
    monkeypatch.setattr(adapter, "schema_head", lambda: 426)
    adapter.require_schema_retired()
    monkeypatch.setattr(adapter, "schema_head", lambda: 425)
    with pytest.raises(RetirementError, match="schema 426"):
        adapter.require_schema_retired()
