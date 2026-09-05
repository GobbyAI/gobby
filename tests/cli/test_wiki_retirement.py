"""Safety and recovery behavior of the one-time legacy wiki retirement procedure."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import pytest

from gobby.storage.hub.postgres import PostgresHubDatabase
from scripts import retire_legacy_wiki as cli
from scripts import wiki_retirement_cargo as cargo
from scripts import wiki_retirement_inventory as inv
from scripts import wiki_retirement_receipts as receipts
from scripts.wiki_retirement_storage import service_identity


class GraphProbe(Protocol):
    def execute_command(self, *args: str) -> Any: ...
    def exists(self, key: str) -> int: ...
    def set(self, key: str, value: str) -> object: ...
    def get(self, key: str) -> bytes | None: ...
    def dump(self, key: str) -> bytes | None: ...
    def restore(self, key: str, ttl: int, data: bytes) -> object: ...
    def delete(self, key: str) -> int: ...
    def close(self) -> None: ...


PROJECT = "d45545c5-ded5-4335-b115-0245752edacf"


class FakeStores:
    def __init__(self, identity: str = "source") -> None:
        self.identity = identity
        self.values = {"qdrant:gwiki_project_example": b"wiki vector content"}
        self.deleted: list[str] = []
        self.fail: str | None = None
        self.head = 425

    def identities(self) -> dict[str, str]:
        return {
            "postgres": self.identity + "-pg",
            "qdrant": self.identity + "-q",
            "falkor": self.identity + "-f",
        }

    def inventory(self) -> list[inv.StoreTarget]:
        return [
            inv.StoreTarget(
                kind=key.split(":", 1)[0],
                name=key.split(":", 1)[1],
                digest=inv.sha(data),
                count=1,
                identity=self.identities()[key.split(":", 1)[0]],
            )
            for key, data in self.values.items()
        ]

    def capture(self, target: inv.StoreTarget) -> bytes | None:
        return self.values.get(target.key)

    def backup(self, target: inv.StoreTarget) -> bytes:
        data = self.capture(target)
        if data is None or inv.sha(data) != target.digest:
            raise inv.RetirementError("Store changed")
        return data

    def delete(self, target: inv.StoreTarget) -> None:
        if self.fail == target.key:
            raise ConnectionError("backend unavailable")
        self.deleted.append(target.key)
        self.values.pop(target.key, None)
        if target.kind == "postgres":
            self.head = 426

    def restore(self, target: inv.StoreTarget, data: bytes) -> None:
        if target.key in self.values and self.values[target.key] != data:
            raise inv.RetirementError("Restore collision")
        self.values[target.key] = data

    def require_schema_pending(self) -> None:
        if self.head != 425:
            raise inv.RetirementError("Wrong predecessor")

    def require_schema_retired(self) -> None:
        if self.head != 426 or "postgres:wiki_schema" in self.values:
            raise inv.RetirementError("Schema postcondition failed")

    def close(self) -> None:
        pass


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[inv.Inventory, Path, FakeStores]:
    repository = tmp_path / "checkout"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.email", "retirement@example.test")
    git(repository, "config", "user.name", "Retirement Test")
    (repository / "original.txt").write_text("Preserve original source")
    git(repository, "add", "original.txt")
    git(repository, "commit", "-qm", "baseline")
    git(repository, "tag", "-am", "archive", inv.ARCHIVE_TAG)
    git(repository, "commit", "--allow-empty", "-qm", "retire wiki")
    (repository / ".gobby").mkdir()
    (repository / ".gobby/project.json").write_text(json.dumps({"id": PROJECT}))
    vault = repository / "wiki"
    (vault / "_gwiki").mkdir(parents=True)
    (vault / "_gwiki/scope.json").write_text(
        json.dumps({"identity": f"project:{PROJECT}", "root": str(vault)})
    )
    (vault / "page.md").write_text("generated wiki page")
    (vault / "mirror.md").symlink_to(repository / "original.txt")
    (vault / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (vault / "run.sh").chmod(0o755)
    home = tmp_path / "gobby"
    home.mkdir(mode=0o700)
    (home / "bootstrap.yaml").write_text("files_home: isolated\n")
    (home / "bin").mkdir()
    for name in ("gcode", "gdaemon", "ghook"):
        (home / "bin" / name).write_text(f"installed {name}")
    files_home = tmp_path / "files"
    files_home.mkdir()
    stores = FakeStores()
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(home / "bin"))
    monkeypatch.setattr(inv, "native_directories", lambda _: [home / "bin"])
    monkeypatch.setattr(cli, "writer_processes", lambda: [])
    value = inv.Inventory(
        created_at=inv.now(),
        repository=str(repository),
        baseline=inv.baseline(repository),
        uid=os.getuid(),
        gobby_home=str(home),
        files_home=str(files_home),
        checkouts=[str(repository)],
        checkout_projects={str(repository): PROJECT},
        native_dirs=[str(home / "bin")],
        roots=inv.discover_roots(repository, home, files_home, [repository]),
        stores=stores.inventory(),
        backend_identities=stores.identities(),
        writers=[],
    )
    root = receipts.prepare_directory(value)
    receipts.write_private(root / "inventory.json", value.model_dump_json().encode())
    return value, root, stores


def ready(
    state: tuple[inv.Inventory, Path, FakeStores],
) -> tuple[receipts.Backup, receipts.RuntimeProof]:
    value, root, stores = state
    backup = cli.create_backup(value, root, stores)
    isolated = FakeStores("isolated")
    isolated.values.clear()
    cli.restore(value, backup, root, stores, isolated, root / "staged")
    rehearsal = cli.rehearse(value, backup, root, stores, isolated)
    assert rehearsal.complete
    binaries = {
        name: receipts.Evidence(
            path=str(Path(value.gobby_home) / "bin" / name),
            digest=inv.sha(inv.read_file(Path(value.gobby_home) / "bin" / name)),
        )
        for name in ("gcode", "gdaemon", "ghook")
    }
    commit = inv.git(Path(value.repository), "rev-parse", "HEAD")
    runtime_digest = inv.sha(
        inv.canonical(
            {
                "installed_repository": value.repository,
                "installed_commit": commit,
                "binaries": {key: item.model_dump() for key, item in binaries.items()},
            }
        )
    )
    evidence = {}
    for name in cli.CHECKS:
        path = root / f"check-{name}.json"
        data = inv.canonical(
            {
                "check": name,
                "inventory_digest": value.digest,
                "runtime_digest": runtime_digest,
                "created_at": inv.now(),
                "command": ["verify", name],
                "exit_code": 0,
                "passed": True,
                "output": f"Verified {name}",
            }
        )
        receipts.write_private(path, data)
        evidence[name] = receipts.Evidence(path=str(path), digest=inv.sha(data))
    proof = receipts.RuntimeProof(
        inventory_digest=value.digest,
        installed_repository=value.repository,
        installed_commit=commit,
        binaries=binaries,
        checks=evidence,
        created_at=inv.now(),
    )
    return backup, proof


def test_apply_preserves_original_and_repeat_is_safe(
    state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, root, stores = state
    backup, proof = ready(state)
    first = cli.apply(value, backup, root, stores, proof)
    second = cli.apply(value, backup, root, stores, proof)
    assert first.complete and second.complete
    assert not (Path(value.repository) / "wiki").exists()
    assert (Path(value.repository) / "original.txt").read_text() == "Preserve original source"
    assert stores.deleted == ["qdrant:gwiki_project_example"]


def test_rehearsal_preserves_modes_links_and_repeats(
    state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, root, stores = state
    backup = cli.create_backup(value, root, stores)
    isolated = FakeStores("isolated")
    isolated.values.clear()
    cli.restore(value, backup, root, stores, isolated, root / "staged")
    result = cli.rehearse(value, backup, root, stores, isolated)
    repeated = cli.rehearse(value, backup, root, stores, isolated)
    assert result.complete and repeated.complete
    restored = root / "rehearsal-after/root-0"
    assert (restored / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (restored / "mirror.md").is_symlink()
    assert os.readlink(restored / "mirror.md") == str(Path(value.repository) / "original.txt")
    assert isolated.values == stores.values


@pytest.mark.parametrize("mutation", ["change", "add", "remove", "new_root"])
def test_inventory_drift_refused_before_deletion(
    state: tuple[inv.Inventory, Path, FakeStores], mutation: str
) -> None:
    value, root, stores = state
    backup, proof = ready(state)
    vault = Path(value.repository) / "wiki"
    if mutation == "change":
        (vault / "page.md").write_text("new user data")
    elif mutation == "add":
        (vault / "new.md").write_text("new artifact")
    elif mutation == "remove":
        (vault / "page.md").unlink()
    else:
        extra = Path(value.repository) / "gobby-wiki"
        (extra / "_gwiki").mkdir(parents=True)
        (extra / "_gwiki/scope.json").write_text("{}")
    with pytest.raises(inv.RetirementError):
        cli.apply(value, backup, root, stores, proof)
    assert stores.deleted == []
    assert vault.is_dir()


def test_partial_backend_failure_retries_remaining_targets(
    state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, root, stores = state
    backup, proof = ready(state)
    stores.fail = "qdrant:gwiki_project_example"
    with pytest.raises(ConnectionError):
        cli.apply(value, backup, root, stores, proof)
    partial = receipts.Receipt.model_validate_json(receipts.read_private(root / "apply.json"))
    assert not partial.complete and partial.items[stores.fail] == "intent"
    assert (Path(value.repository) / "wiki/page.md").exists()
    stores.fail = None
    assert cli.apply(value, backup, root, stores, proof).complete


def test_interruption_after_unlink_recovers_from_intent(
    state: tuple[inv.Inventory, Path, FakeStores], monkeypatch: pytest.MonkeyPatch
) -> None:
    value, root, stores = state
    backup, proof = ready(state)
    unlink = os.unlink
    interrupted = False

    def interrupt(path: str, *, dir_fd: int | None = None) -> None:
        nonlocal interrupted
        unlink(path, dir_fd=dir_fd)
        if str(path).startswith("quarantine-") and not interrupted:
            interrupted = True
            raise InterruptedError("simulated crash after unlink")

    monkeypatch.setattr(os, "unlink", interrupt)
    with pytest.raises(InterruptedError):
        cli.apply(value, backup, root, stores, proof)
    monkeypatch.setattr(os, "unlink", unlink)
    assert cli.apply(value, backup, root, stores, proof).complete
    assert stores.deleted == ["qdrant:gwiki_project_example"]


def test_quarantine_preserves_racing_replacement(
    state: tuple[inv.Inventory, Path, FakeStores], monkeypatch: pytest.MonkeyPatch
) -> None:
    value, root, stores = state
    backup, proof = ready(state)
    original_rename = os.rename
    target = Path(value.repository) / "wiki/run.sh"
    raced = False

    def race(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal raced
        if source == "run.sh" and not raced:
            target.write_text("unrelated replacement survives")
            raced = True
        original_rename(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", race)
    with pytest.raises(inv.RetirementError, match="Changed entry preserved"):
        cli.apply(value, backup, root, stores, proof)
    assert target.read_text() == "unrelated replacement survives"


def test_symlink_parent_is_never_followed(
    state: tuple[inv.Inventory, Path, FakeStores], tmp_path: Path
) -> None:
    value, _, _ = state
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    link = Path(value.repository) / "redirect"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        inv.scan(link / "keep.txt")
    assert (outside / "keep.txt").read_text() == "keep"


def test_out_of_scope_inventory_rejected(state: tuple[inv.Inventory, Path, FakeStores]) -> None:
    value, _, _ = state
    altered = value.roots[0].model_copy(update={"path": value.repository})
    with pytest.raises(inv.RetirementError, match="Out-of-scope"):
        inv.validate_inventory(value.model_copy(update={"roots": [altered]}))


def test_wrong_scope_marker_refused(state: tuple[inv.Inventory, Path, FakeStores]) -> None:
    value, _, _ = state
    marker = Path(value.repository) / "wiki/_gwiki/scope.json"
    marker.write_text("{}")
    with pytest.raises(inv.RetirementError, match="scope identity"):
        inv.discover_roots(
            Path(value.repository),
            Path(value.gobby_home),
            Path(value.files_home),
            [Path(value.repository)],
        )


def test_backup_is_private_and_tampering_refused(
    state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, root, stores = state
    backup = cli.create_backup(value, root, stores)
    assert root.stat().st_mode & 0o777 == 0o700
    assert all(
        (root / artifact.path).stat().st_mode & 0o777 == 0o600
        for artifact in backup.artifacts.values()
    )
    artifact = next(iter(backup.artifacts.values()))
    receipts.write_private(root / artifact.path, b"changed recovery bytes")
    with pytest.raises(inv.RetirementError, match="artifact changed"):
        cli.validate_backup(value, root, backup)


def test_same_backend_rehearsal_refused(state: tuple[inv.Inventory, Path, FakeStores]) -> None:
    value, root, stores = state
    backup = cli.create_backup(value, root, stores)
    with pytest.raises(inv.RetirementError, match="isolated"):
        cli.rehearse(value, backup, root, stores, FakeStores())
    assert stores.deleted == []


def test_runtime_proof_rejects_unbound_checks(
    state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, root, stores = state
    backup, proof = ready(state)
    check = proof.checks["mcp_absent"]
    path = Path(check.path)
    data = json.loads(receipts.read_private(path))
    data["runtime_digest"] = "different runtime"
    payload = inv.canonical(data)
    receipts.write_private(path, payload)
    changed = proof.model_copy(
        update={
            "checks": {
                **proof.checks,
                "mcp_absent": check.model_copy(update={"digest": inv.sha(payload)}),
            }
        }
    )
    with pytest.raises(inv.RetirementError, match="installed runtime"):
        cli.apply(value, backup, root, stores, changed)
    assert stores.deleted == []


def test_deferred_schema_requires_exact_postcondition(
    state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, old_root, stores = state
    stores.values["postgres:wiki_schema"] = b"five wiki tables"
    value = value.model_copy(update={"stores": stores.inventory()})
    root = receipts.prepare_directory(value)
    backup, proof = ready((value, root, stores))
    pending = cli.apply(value, backup, root, stores, proof, defer_schema=True)
    assert pending.deferred_schema and not pending.complete
    assert pending.items["postgres:wiki_schema"] == "intent"
    stores.values.pop("postgres:wiki_schema")
    stores.head = 427
    with pytest.raises(inv.RetirementError, match="postcondition"):
        cli.apply(value, backup, root, stores, proof)
    stores.head = 426
    assert cli.apply(value, backup, root, stores, proof).complete
    assert old_root.exists()


@pytest.mark.parametrize(
    "name", ["gobby:wiki-prune", "codewiki-nightly", "wiki-overview-injection", "gobby:wiki-recap"]
)
def test_installed_legacy_writer_names_are_admitted(name: str) -> None:
    assert inv.WIKI_NAME.search(name)


def test_inventory_default_never_calls_mutation(
    state: tuple[inv.Inventory, Path, FakeStores],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value, _, stores = state
    monkeypatch.setattr(cli, "configured_stores", lambda: stores)
    result = cli.main(
        [
            "--repository",
            value.repository,
            "--gobby-home",
            value.gobby_home,
            "--files-home",
            value.files_home,
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert result == 0 and output["roots"] == 1
    assert stores.deleted == []
    assert (Path(value.repository) / "wiki/page.md").exists()


@pytest.mark.integration
def test_isolated_falkor_serialization_and_endpoint_aliases() -> None:
    """Exercise installed graph module without accessing live port16379."""
    if os.environ.get("GOBBY_RETIRE_TEST_SERVICES") != "1":
        pytest.skip("Dedicated retirement services requested explicitly")
    from redis import Redis

    client = cast(GraphProbe, Redis(host="127.0.0.1", port=16389, socket_timeout=10))
    graph = "gobby_wiki"
    unrelated = "retirement_unrelated_21836"
    assert not client.exists(graph) and not client.exists(unrelated)
    try:
        client.execute_command(
            "GRAPH.QUERY",
            graph,
            'CREATE (:WikiDoc {path:"a.md"})-[:LINKS_TO {kind:"wiki"}]->(:WikiDoc {path:"b.md"})',
        )
        client.set(unrelated, "preserve")
        data = client.dump(graph)
        assert isinstance(data, bytes)
        before = client.execute_command(
            "GRAPH.QUERY", graph, "MATCH (a)-[r]->(b) RETURN a.path,type(r),r.kind,b.path"
        )
        client.execute_command("GRAPH.DELETE", graph)
        client.restore(graph, 0, data)
        after = client.execute_command(
            "GRAPH.QUERY", graph, "MATCH (a)-[r]->(b) RETURN a.path,type(r),r.kind,b.path"
        )
        assert before[:2] == after[:2]
        assert client.get(unrelated) == b"preserve"
        assert service_identity("redis://localhost:16389", 6379, isolated=True) == service_identity(
            "redis://127.0.0.1:16389", 6379, isolated=True
        )
    finally:
        if client.exists(graph):
            client.execute_command("GRAPH.DELETE", graph)
        client.delete(unrelated)
        client.close()


@pytest.mark.integration
def test_falkor_semantic_fingerprint_and_exact_dump_recovery() -> None:
    if os.environ.get("GOBBY_RETIRE_TEST_SERVICES") != "1":
        pytest.skip("Dedicated retirement services requested explicitly")
    from scripts.wiki_retirement_storage import Datastores

    stores = Datastores(
        "postgresql://gobby_test:gobby_test@127.0.0.1:60893/gobby_test",
        "http://127.0.0.1:6338",
        "redis://127.0.0.1:16389",
        isolated=True,
    )
    graph = cast(GraphProbe, stores.redis)
    unrelated = "retirement_semantic_survivor_21836"
    assert not graph.exists(inv.GRAPH) and not graph.exists(unrelated)
    try:
        graph.execute_command(
            "GRAPH.QUERY",
            inv.GRAPH,
            'UNWIND range(0,255) AS i CREATE (:WikiDoc:Generated {path:toString(i), rank:i, score:0.5, tags:["a","b"], active:true})',
        )
        graph.execute_command(
            "GRAPH.QUERY",
            inv.GRAPH,
            'MATCH (a:WikiDoc),(b:WikiDoc) WHERE a.rank=0 AND b.rank=1 CREATE (a)-[:LINKS_TO {kind:"wiki", weight:2.0}]->(b)',
        )
        graph.execute_command("GRAPH.QUERY", inv.GRAPH, "CREATE INDEX FOR (n:WikiDoc) ON (n.path)")
        graph.execute_command(
            "GRAPH.CONSTRAINT",
            "CREATE",
            inv.GRAPH,
            "MANDATORY",
            "NODE",
            "WikiDoc",
            "PROPERTIES",
            "1",
            "path",
        )
        graph.set(unrelated, "preserve other graph state")
        data = stores._falkor_snapshot()
        assert data is not None
        for _ in range(3):
            assert stores._falkor_snapshot() == data
        value = json.loads(data)
        assert len(value["nodes"]) == 256 and len(value["edges"]) == 1
        assert value["indexes"] and value["constraints"]
        target = inv.StoreTarget(
            kind="falkor", name=inv.GRAPH, digest=inv.sha(data), count=1, identity="isolated"
        )
        backup = stores.backup(target)
        for mutation in (
            "MATCH (n:WikiDoc) WHERE n.rank=0 SET n.rank=999",
            'MATCH ()-[r:LINKS_TO]->() SET r.kind="changed"',
            "MATCH (n:WikiDoc) WHERE n.rank=0 REMOVE n:Generated",
        ):
            graph.execute_command("GRAPH.QUERY", inv.GRAPH, mutation)
            assert stores.capture(target) != data
            with pytest.raises(inv.RetirementError, match="changed since inventory"):
                stores.backup(target)
            stores.delete(target)
            stores.restore(target, backup)
            assert stores.capture(target) == data
        graph.execute_command("GRAPH.QUERY", inv.GRAPH, "DROP INDEX ON :WikiDoc(path)")
        assert stores.capture(target) != data
        stores.delete(target)
        stores.restore(target, backup)
        assert stores.capture(target) == data
        graph.execute_command(
            "GRAPH.CONSTRAINT",
            "DROP",
            inv.GRAPH,
            "MANDATORY",
            "NODE",
            "WikiDoc",
            "PROPERTIES",
            "1",
            "path",
        )
        assert stores.capture(target) != data
        stores.delete(target)
        stores.restore(target, backup)
        assert stores.capture(target) == data
        assert graph.get(unrelated) == b"preserve other graph state"
    finally:
        if graph.exists(inv.GRAPH):
            graph.execute_command("GRAPH.DELETE", inv.GRAPH)
        graph.delete(unrelated)
        stores.close()


@pytest.mark.integration
def test_isolated_postgres_qdrant_roundtrip_preserves_other_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("GOBBY_RETIRE_TEST_SERVICES") != "1":
        pytest.skip("Dedicated retirement services requested explicitly")
    from uuid import uuid4

    from psycopg import sql
    from qdrant_client import models

    from scripts.wiki_retirement_storage import Datastores

    schema = "retirement_" + uuid4().hex
    collection = "gwiki_project_" + uuid4().hex
    unrelated = "gcode_project_retirement_" + uuid4().hex
    monkeypatch.setenv(
        "GOBBY_RETIRE_ISOLATED_POSTGRES_CONTAINER", "gobby-wiki-retirement-21771-postgres"
    )
    stores = Datastores(
        "postgresql://gobby_test:gobby_test@127.0.0.1:60893/gobby_test",
        "http://127.0.0.1:6338",
        "redis://127.0.0.1:16389",
        schema=schema,
        isolated=True,
    )
    try:
        stores.pg.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        for table in inv.PG_TABLES:
            stores.pg.execute(
                sql.SQL(
                    "CREATE TABLE {}.{} (id text PRIMARY KEY, scope_kind text, scope_id text, body text)"
                ).format(sql.Identifier(schema), sql.Identifier(table))
            )
            stores.pg.execute(
                sql.SQL(
                    "INSERT INTO {}.{} VALUES ('wiki-row','project','test-scope','wiki content')"
                ).format(sql.Identifier(schema), sql.Identifier(table))
            )
        stores.pg.execute(
            sql.SQL("CREATE TABLE {}.unrelated (body text)").format(sql.Identifier(schema))
        )
        stores.pg.execute(
            sql.SQL(
                "INSERT INTO {}.unrelated VALUES ('preserve canonical summaries and grant reservations')"
            ).format(sql.Identifier(schema))
        )
        stores.pg.commit()
        identities = stores.identities()
        snapshot = stores._pg_snapshot()
        pg_target = inv.StoreTarget(
            kind="postgres",
            name="wiki_schema",
            digest=inv.sha(snapshot),
            count=5,
            identity=identities["postgres"],
        )
        backup = stores.backup(pg_target)
        for table in inv.PG_TABLES:
            stores.pg.execute(
                sql.SQL("DROP TABLE {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
            )
        stores.pg.commit()
        stores.restore(pg_target, backup)
        assert stores._pg_snapshot() == snapshot
        for table in inv.PG_TABLES:
            stores.pg.execute(
                sql.SQL("DELETE FROM {}.{} WHERE id='wiki-row'").format(
                    sql.Identifier(schema), sql.Identifier(table)
                )
            )
        stores.pg.commit()
        stores.restore(pg_target, backup)
        assert stores._pg_snapshot() == snapshot
        preserved = stores.pg.execute(
            sql.SQL("SELECT body FROM {}.unrelated").format(sql.Identifier(schema))
        ).fetchone()
        assert preserved == ("preserve canonical summaries and grant reservations",)
        stores.pg.rollback()
        for name in (collection, unrelated):
            stores.qdrant.create_collection(
                name, vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE)
            )
        point_id = str(uuid4())
        stores.qdrant.upsert(
            collection,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "namespace": "gwiki",
                        "scope_kind": "project",
                        "scope_id": collection.removeprefix("gwiki_project_"),
                        "content": "wiki chunk",
                    },
                )
            ],
            wait=True,
        )
        stores.qdrant.upsert(
            unrelated,
            points=[
                models.PointStruct(
                    id=str(uuid4()),
                    vector=[0.0, 1.0, 0.0],
                    payload={"namespace": "gcode", "content": "preserve code graph"},
                )
            ],
            wait=True,
        )
        vector_snapshot = stores._qdrant_snapshot(collection)
        assert vector_snapshot is not None
        vector_target = inv.StoreTarget(
            kind="qdrant",
            name=collection,
            digest=inv.sha(vector_snapshot),
            count=1,
            identity=identities["qdrant"],
        )
        vector_backup = stores.backup(vector_target)
        stores.delete(vector_target)
        assert not stores.qdrant.collection_exists(collection)
        stores.restore(vector_target, vector_backup)
        assert stores._qdrant_snapshot(collection) == vector_snapshot
        assert stores.qdrant.count(unrelated, exact=True).count == 1
        assert (
            service_identity("http://localhost:6338", 6333, isolated=True) == identities["qdrant"]
        )
    finally:
        for name in (collection, unrelated):
            if stores.qdrant.collection_exists(name):
                stores.qdrant.delete_collection(name)
        stores.pg.rollback()
        stores.pg.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
        stores.pg.commit()
        stores.close()


def test_rehearsal_resumes_after_backend_interruption(
    state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, root, stores = state
    backup = cli.create_backup(value, root, stores)
    isolated = FakeStores("isolated")
    isolated.fail = "qdrant:gwiki_project_example"
    with pytest.raises(ConnectionError):
        cli.rehearse(value, backup, root, stores, isolated)
    isolated.fail = None
    assert cli.rehearse(value, backup, root, stores, isolated).complete
    assert isolated.values == stores.values
    assert (Path(value.repository) / "wiki/page.md").read_text() == "generated wiki page"


def test_registry_compare_delete_rejects_new_content(postgres_db: PostgresHubDatabase) -> None:
    from gobby.storage.definitions.rules import RuleDefinitionManager
    from scripts.wiki_retirement_storage import Datastores

    manager = RuleDefinitionManager(postgres_db)
    wiki = manager.create("wiki-retirement-test", {"event": "session-start"})
    survivor = manager.create("memory-retirement-survivor", {"event": "session-start"})
    schema_row = postgres_db.fetchone("SELECT current_schema() AS name")
    assert schema_row is not None
    schema = schema_row["name"]
    stores = Datastores(
        postgres_db.conninfo,
        "http://127.0.0.1:6338",
        "redis://127.0.0.1:16389",
        schema=schema,
        isolated=True,
    )
    try:
        data = stores._registry_snapshot("rule", wiki.id)
        assert data is not None
        target = inv.StoreTarget(
            kind="rule", name=wiki.id, digest=inv.sha(data), count=1, identity="fixture"
        )
        manager.update(wiki.id, description="new content outside the backup")
        with pytest.raises(inv.RetirementError, match="Registry changed before locked deletion"):
            stores.delete(target)
        assert manager.get(wiki.id).description == "new content outside the backup"
        current = stores._registry_snapshot("rule", wiki.id)
        assert current is not None
        stores.delete(target.model_copy(update={"digest": inv.sha(current)}))
        assert stores._registry_snapshot("rule", wiki.id) is None
        assert manager.get(survivor.id).name == "memory-retirement-survivor"
    finally:
        stores.close()


@pytest.mark.integration
@pytest.mark.parametrize("existing_references", [False, True])
def test_isolated_project_scoped_cron_restore(
    postgres_db: PostgresHubDatabase,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    existing_references: bool,
) -> None:
    from uuid import uuid4

    import psycopg
    from psycopg.conninfo import make_conninfo

    from gobby.identity import hash_password
    from gobby.storage.cron import CronJobStorage
    from gobby.storage.definitions.rules import RuleDefinitionManager
    from gobby.storage.definitions.variables import SessionVariableDefaultManager
    from gobby.storage.machines import LocalMachineManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.schema_contract import apply_schema
    from gobby.storage.users import LocalUserManager
    from scripts.wiki_retirement_storage import Datastores
    from tests.fixtures.postgres import isolated_test_schema

    projects = LocalProjectManager(postgres_db)
    project = projects.create("Original source project")
    rule_project = projects.create("Original rule project")
    variable_project = projects.create("Original variable project")
    password = hash_password("Isolated-retirement-fixture-password-21857!")
    owner = LocalUserManager(postgres_db).create(
        name="Original source owner",
        email="retirement-source@example.invalid",
        password_hash=password,
    )
    machine_id = str(uuid4())
    LocalMachineManager(postgres_db).upsert_seen(machine_id, owner.id, label="Original machine")
    monkeypatch.setattr("gobby.storage.cron_runs.get_machine_id", lambda: machine_id)
    cron = CronJobStorage(postgres_db)
    job = cron.create_job(
        project.id,
        "wiki-retirement-project-cron",
        "interval",
        "handler",
        {"handler": "wiki-retirement-fixture"},
        interval_seconds=3600,
        enabled=False,
    )
    run = cron.create_run(job.id)
    assert run is not None
    rule = RuleDefinitionManager(postgres_db).create(
        "wiki-retirement-project-rule", {"event": "session-start"}, project_id=rule_project.id
    )
    variable = SessionVariableDefaultManager(postgres_db).create(
        "wiki_retirement_project_variable", False, project_id=variable_project.id
    )
    schema_row = postgres_db.fetchone("SELECT current_schema() AS name")
    assert schema_row is not None

    def identity_rows(db: PostgresHubDatabase) -> list[list[Any]]:
        return [
            db.fetchall(f"SELECT to_jsonb(t) AS row FROM {table} t ORDER BY id")
            for table in ("projects", "machines", "users")
        ]

    original_identities = identity_rows(postgres_db)
    source = Datastores(
        postgres_db.conninfo,
        "http://127.0.0.1:6338",
        "redis://127.0.0.1:16389",
        schema=schema_row["name"],
        isolated=True,
    )
    backups: list[tuple[inv.StoreTarget, bytes]] = []
    try:
        for kind, name in (("job", job.id), ("rule", rule.id), ("variable", variable.id)):
            data = source._registry_snapshot(kind, name)
            assert data is not None
            target = inv.StoreTarget(
                kind=kind, name=name, digest=inv.sha(data), count=1, identity="source-fixture"
            )
            backups.append((target, source.backup(target)))
        with isolated_test_schema(postgres_database_url, "wikirefs") as schema:
            apply_schema(postgres_database_url, schema=schema)
            conninfo = make_conninfo(
                postgres_database_url, options=f"-csearch_path={schema} -ctimezone=UTC"
            )
            target_db = PostgresHubDatabase(conninfo)
            target_stores = Datastores(
                conninfo,
                "http://127.0.0.1:6338",
                "redis://127.0.0.1:16389",
                schema=schema,
                isolated=True,
            )
            try:
                target_projects = LocalProjectManager(target_db)
                target_projects.create("Unrelated target project")
                if existing_references:
                    target_projects.create("Existing target project", project_id=project.id)
                    target_owner = LocalUserManager(target_db).create(
                        name="Existing target owner",
                        email="retirement-target@example.invalid",
                        password_hash=password,
                    )
                    LocalMachineManager(target_db).upsert_seen(
                        machine_id, target_owner.id, label="Existing target machine"
                    )
                before = identity_rows(target_db)
                insert_row = target_stores._insert_row

                def interrupt_run_insert(
                    db: PostgresHubDatabase, table: str, row: dict[str, Any]
                ) -> None:
                    if table == "cron_runs":
                        raise InterruptedError("Interrupted after definition and references")
                    insert_row(db, table, row)

                with monkeypatch.context() as patch:
                    patch.setattr(target_stores, "_insert_row", interrupt_run_insert)
                    with pytest.raises(InterruptedError, match="after definition"):
                        target_stores.restore(*backups[0])
                assert identity_rows(target_db) == before
                assert target_stores.capture(backups[0][0]) is None
                for target, data in backups:
                    target_stores.restore(target, data)
                    assert target_stores.capture(target) == data
                restored_identities = identity_rows(target_db)
                for target, data in backups:
                    target_stores.restore(target, data)
                assert identity_rows(target_db) == restored_identities
                for before_rows, after_rows in zip(before, restored_identities, strict=True):
                    assert all(row in after_rows for row in before_rows)
                assert LocalUserManager(target_db).get(owner.id) is None
                restored_machine = LocalMachineManager(target_db).get(machine_id)
                assert restored_machine is not None
                if not existing_references:
                    assert restored_machine.owner_user_id != owner.id
                    assert restored_machine.label == "Wiki retirement rehearsal"
                    assert len(restored_identities[0]) == len(before[0]) + 3
                    assert len(restored_identities[1]) == len(before[1]) + 1
                    assert len(restored_identities[2]) == len(before[2]) + 1
                with pytest.raises(psycopg.errors.ForeignKeyViolation):
                    with target_db.transaction() as txn:
                        txn.execute(
                            "UPDATE cron_runs SET machine_id=%s WHERE id=%s", (str(uuid4()), run.id)
                        )
                assert target_stores.capture(backups[0][0]) == backups[0][1]
                assert identity_rows(postgres_db) == original_identities
                for target, data in backups:
                    assert source.capture(target) == data
            finally:
                target_stores.close()
                target_db.close()
    finally:
        source.close()


def test_legacy_mirror_admits_children_and_preserves_checkout(
    state: tuple[inv.Inventory, Path, FakeStores],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, _, stores = state
    repository = Path(value.repository)
    mirror = tmp_path / "legacy-checkout"
    git(repository, "worktree", "add", "-qb", "wiki", str(mirror))
    (mirror / "original.txt").unlink()
    (mirror / "_gwiki").mkdir()
    source_marker = repository / "wiki/_gwiki/scope.json"
    (mirror / "_gwiki/scope.json").write_bytes(source_marker.read_bytes())
    (mirror / "knowledge").mkdir()
    (mirror / "knowledge/page.md").write_text("mirrored wiki content")
    roots = inv.discover_roots(
        repository, Path(value.gobby_home), Path(value.files_home), [repository, mirror]
    )
    mirrored = [root for root in roots if root.kind == "mirror_entry"]
    assert {Path(root.path).name for root in mirrored} == {"_gwiki", "knowledge"}
    assert all(Path(root.path) != mirror and Path(root.path).name != ".git" for root in mirrored)
    modified = mirrored[0].model_copy(update={"path": str(mirror / "unrelated.txt")})
    altered = value.model_copy(
        update={"checkouts": [str(repository), str(mirror)], "roots": [modified]}
    )
    with pytest.raises(inv.RetirementError, match="Out-of-scope"):
        inv.validate_inventory(altered)
    value = value.model_copy(update={"checkouts": [str(repository), str(mirror)], "roots": roots})
    root = receipts.prepare_directory(value)
    backup, proof = ready((value, root, stores))
    original_mark = receipts.Journal.mark
    marker_key = f"file:{mirror / '_gwiki'}:."

    def interrupt_after_marker(
        journal: receipts.Journal, key: str, status: Literal["intent", "done"]
    ) -> None:
        original_mark(journal, key, status)
        if key == marker_key and status == "done":
            monkeypatch.setattr(receipts.Journal, "mark", original_mark)
            raise InterruptedError("mirror marker deleted")

    monkeypatch.setattr(receipts.Journal, "mark", interrupt_after_marker)
    with pytest.raises(InterruptedError, match="mirror marker deleted"):
        cli.apply(value, backup, root, stores, proof)
    assert not (mirror / "_gwiki").exists()
    assert (mirror / "knowledge/page.md").exists()
    assert cli.apply(value, backup, root, stores, proof).complete
    assert (mirror / ".git").is_file()
    assert not (mirror / "knowledge").exists()
    assert (repository / "original.txt").read_text() == "Preserve original source"


def test_mirror_with_matching_project_in_foreign_repository_refused(
    state: tuple[inv.Inventory, Path, FakeStores], tmp_path: Path
) -> None:
    value, _, _ = state
    repository = Path(value.repository)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-qb", "wiki")
    (foreign / ".gobby").mkdir()
    (foreign / ".gobby/project.json").write_text(json.dumps({"id": PROJECT}))
    (foreign / "_gwiki").mkdir()
    (foreign / "_gwiki/scope.json").write_bytes(
        (repository / "wiki/_gwiki/scope.json").read_bytes()
    )
    with pytest.raises(inv.RetirementError, match="different repository"):
        inv.discover_roots(
            repository, Path(value.gobby_home), Path(value.files_home), [repository, foreign]
        )


def test_rehearsal_checkpoint_cannot_redirect_deletion(
    state: tuple[inv.Inventory, Path, FakeStores], tmp_path: Path
) -> None:
    value, root, stores = state
    backup = cli.create_backup(value, root, stores)
    isolated = FakeStores("isolated")
    isolated.fail = "qdrant:gwiki_project_example"
    with pytest.raises(ConnectionError):
        cli.rehearse(value, backup, root, stores, isolated)
    outside = tmp_path / "unrelated-directory"
    outside.mkdir()
    (outside / "keep.txt").write_text("preserve unowned content")
    path = root / "rehearsal-trees.json"
    checkpoint = json.loads(receipts.read_private(path))
    checkpoint["roots"][0]["path"] = str(outside)
    receipts.write_private(path, inv.canonical(checkpoint))
    isolated.fail = None
    with pytest.raises(inv.RetirementError, match="exact private destination"):
        cli.rehearse(value, backup, root, stores, isolated)
    assert (outside / "keep.txt").read_text() == "preserve unowned content"
    assert isolated.deleted == []


@pytest.mark.parametrize("field", ["inventory_digest", "backup_digest", "entries"])
def test_rehearsal_checkpoint_rejects_changed_binding(
    state: tuple[inv.Inventory, Path, FakeStores], field: str
) -> None:
    value, root, stores = state
    backup = cli.create_backup(value, root, stores)
    isolated = FakeStores("isolated")
    isolated.fail = "qdrant:gwiki_project_example"
    with pytest.raises(ConnectionError):
        cli.rehearse(value, backup, root, stores, isolated)
    path = root / "rehearsal-trees.json"
    checkpoint = json.loads(receipts.read_private(path))
    if field == "entries":
        checkpoint["roots"][0]["entries"][0]["mode"] ^= 0o100
    else:
        checkpoint[field] = "different binding"
    receipts.write_private(path, inv.canonical(checkpoint))
    isolated.fail = None
    with pytest.raises(inv.RetirementError, match="checkpoint"):
        cli.rehearse(value, backup, root, stores, isolated)
    assert isolated.deleted == []


def test_runtime_native_directory_must_match_inventory(
    state: tuple[inv.Inventory, Path, FakeStores], monkeypatch: pytest.MonkeyPatch
) -> None:
    value, root, stores = state
    backup, proof = ready(state)
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(root / "different-install"))
    with pytest.raises(inv.RetirementError, match="native directory"):
        cli.apply(value, backup, root, stores, proof)
    assert stores.deleted == []


def test_module_entrypoint_help() -> None:
    result = subprocess.run(
        ["uv", "run", "python", "-m", "scripts.retire_legacy_wiki", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--inventory" in result.stdout and "--runtime-proof" in result.stdout


@pytest.fixture
def cargo_state(
    state: tuple[inv.Inventory, Path, FakeStores], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[inv.Inventory, Path, FakeStores]:
    value, _, stores = state
    home = tmp_path / "cargo"
    (home / "bin").mkdir(parents=True)
    for name in ("gwiki", "survivor"):
        (home / "bin" / name).write_text(f"#!/bin/sh\n# {name}\nexit 0\n")
        (home / "bin" / name).chmod(0o755)
    package = f"gobby-wiki 0.8.0 (path+file://{value.repository}/crates/gwiki)"
    survivor = "survivor 1.0.0 (registry+https://github.com/rust-lang/crates.io-index)"
    installs = {}
    for key, name in ((package, "gwiki"), (survivor, "survivor")):
        installs[key] = {
            "version_req": None,
            "bins": [name],
            "features": [],
            "all_features": False,
            "no_default_features": False,
            "profile": "release",
            "target": "aarch64-apple-darwin",
            "rustc": "rustc",
        }
    (home / ".crates2.json").write_bytes(inv.canonical({"installs": installs}))
    (home / ".crates.toml").write_text(
        "[v1]\n"
        + json.dumps(package)
        + ' = ["gwiki"]\n'
        + json.dumps(survivor)
        + ' = ["survivor"]\n'
    )
    native_dirs = [Path(value.gobby_home) / "bin", home / "bin"]
    monkeypatch.setattr(inv, "native_directories", lambda _: native_dirs)
    monkeypatch.setattr(cli, "native_directories", lambda _: native_dirs)
    value = cli.inventory(
        Path(value.repository), Path(value.gobby_home), Path(value.files_home), stores, []
    )
    assert not value.errors and len(value.cargo_installs) == 1
    root = receipts.prepare_directory(value)
    return value, root, stores


def test_cargo_uninstall_preserves_other_packages_and_recovery(
    cargo_state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, root, stores = cargo_state
    item = value.cargo_installs[0]
    home = Path(item.home)
    backup, proof = ready(cargo_state)
    selected = json.loads(receipts.artifact_bytes(root, backup.artifacts[item.key]))
    assert selected == {"package": item.package, "metadata": item.metadata}
    assert "survivor" not in inv.canonical(selected).decode()
    assert (root / "cargo-after-0/bin/gwiki").stat().st_mode & 0o777 == 0o755
    with pytest.raises(inv.RetirementError, match="quiet-window"):
        cli.apply(value, backup, root, stores, proof)
    assert stores.deleted == []
    assert cli.apply(value, backup, root, stores, proof, cargo_quiet_window=True).complete
    assert cli.apply(value, backup, root, stores, proof, cargo_quiet_window=True).complete
    assert not (home / "bin/gwiki").exists()
    assert (home / "bin/survivor").read_text() == "#!/bin/sh\n# survivor\nexit 0\n"
    assert not cargo.verify_cargo(item)


def test_cargo_retry_after_uninstall_preserves_survivors(
    cargo_state: tuple[inv.Inventory, Path, FakeStores], monkeypatch: pytest.MonkeyPatch
) -> None:
    value, root, stores = cargo_state
    backup, proof = ready(cargo_state)
    real_run = subprocess.run

    def interrupt(*args: Any, **kwargs: Any) -> Any:
        result = real_run(*args, **kwargs)
        if args[0][0] == "cargo":
            monkeypatch.setattr(subprocess, "run", real_run)
            raise InterruptedError("Cargo exited before receipt")
        return result

    monkeypatch.setattr(subprocess, "run", interrupt)
    with pytest.raises(InterruptedError, match="before receipt"):
        cli.apply(value, backup, root, stores, proof, cargo_quiet_window=True)
    assert cli.apply(value, backup, root, stores, proof, cargo_quiet_window=True).complete
    assert not cargo.verify_cargo(value.cargo_installs[0])


@pytest.mark.parametrize("mismatch", ["source", "metadata", "binary"])
def test_cargo_refuses_changed_ownership_or_binary(
    cargo_state: tuple[inv.Inventory, Path, FakeStores], mismatch: str
) -> None:
    value, root, stores = cargo_state
    backup, proof = ready(cargo_state)
    item = value.cargo_installs[0]
    home = Path(item.home)
    if mismatch == "binary":
        (home / "bin/gwiki").write_text("replacement binary")
    elif mismatch == "metadata":
        (home / ".crates.toml").write_text("[v1]\n")
    else:
        foreign = item.package.replace(value.repository, "/unrecorded/project")
        item = item.model_copy(update={"package": foreign})
        with pytest.raises(inv.RetirementError, match="recorded checkout"):
            cargo.source_version(
                item.package, Path(value.repository), list(map(Path, value.checkouts))
            )
        return
    with pytest.raises(inv.RetirementError, match="changed|partially removed"):
        cli.apply(value, backup, root, stores, proof, cargo_quiet_window=True)
    assert (home / "bin/gwiki").exists()


def test_cargo_symlink_is_unlinked_without_changing_original(
    cargo_state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, _, stores = cargo_state
    item = value.cargo_installs[0]
    binary = Path(item.home) / "bin/gwiki"
    binary.unlink()
    original = Path(value.repository) / "original.txt"
    binary.symlink_to(original)
    value = cli.inventory(
        Path(value.repository), Path(value.gobby_home), Path(value.files_home), stores, []
    )
    root = receipts.prepare_directory(value)
    backup, proof = ready((value, root, stores))
    assert cli.apply(value, backup, root, stores, proof, cargo_quiet_window=True).complete
    assert not binary.is_symlink()
    assert original.read_text() == "Preserve original source"


def test_cargo_source_in_other_recorded_repository_is_refused(
    cargo_state: tuple[inv.Inventory, Path, FakeStores], tmp_path: Path
) -> None:
    value, _, _ = cargo_state
    foreign = tmp_path / "foreign-repository"
    foreign.mkdir()
    git(foreign, "init", "-q")
    package = value.cargo_installs[0].package.replace(value.repository, str(foreign))
    with pytest.raises(inv.RetirementError, match="different repository"):
        cargo.source_version(package, Path(value.repository), [Path(value.repository), foreign])


def test_cargo_registration_without_binary_is_not_silently_skipped(
    cargo_state: tuple[inv.Inventory, Path, FakeStores],
) -> None:
    value, _, stores = cargo_state
    (Path(value.cargo_installs[0].home) / "bin/gwiki").unlink()
    current = cli.inventory(
        Path(value.repository), Path(value.gobby_home), Path(value.files_home), stores, []
    )
    assert any("missing binary" in message for message in current.errors)
