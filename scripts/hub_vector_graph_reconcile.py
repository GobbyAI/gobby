#!/usr/bin/env python3
"""One-time Qdrant/FalkorDB inventory and orphan reconciliation campaign."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess  # nosec B404 - fixed owner CLI argv, never shell=True
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import click
import redis
from qdrant_client import QdrantClient

from gobby.cli.hub_backup._manifest import load_manifest
from gobby.cli.hub_maintenance import (
    _resolve_database_url,
    register_campaign_executor,
)
from gobby.cli.runtime import CliRuntime, get_cli_runtime
from gobby.config.app import DaemonConfig
from gobby.paths import get_gobby_home
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.maintenance_epoch import (
    DestructiveBatch,
    MaintenanceEpoch,
    get_destructive_batch,
    run_receipted_component,
)

_CANDIDATE_FORMAT = "gobby-vector-graph-reconcile-candidates"
_DELETION_FORMAT = "gobby-vector-graph-reconcile-deletion"
_MANIFEST_VERSION = 1
_QDRANT_RESERVED = frozenset({"memories", "tool_embeddings"})
_FALKOR_RESERVED = frozenset({"gobby_code", "gobby_wiki", "gobby_kg", "gwiki"})
_CODE_STANDALONE_PREFIX = "code_symbols_graph-standalone-"
_RECALL_GRAPH_PATTERNS = (
    re.compile(r"^test_recall_benchmark_.+$"),
    re.compile(r"^test_recall_benchmark_e2e_.+$"),
    re.compile(r"^dbg.*_.*$"),
    re.compile(r"^probe_cluster_.+$"),
)


@dataclass(frozen=True, slots=True)
class Inventory:
    qdrant: frozenset[str]
    falkordb: frozenset[str]

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "qdrant": sorted(self.qdrant),
            "falkordb": sorted(self.falkordb),
        }


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    project_ids: frozenset[str]
    code_indexed_project_ids: frozenset[str]
    wiki_topics: frozenset[str]


class _RedisCommandClient(Protocol):
    def execute_command(self, *args: str) -> object: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    store: str
    namespace: str
    tier: int
    disposition: str
    reason: str
    owner: str | None = None
    project_id: str | None = None
    topic: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "store": self.store,
            "namespace": self.namespace,
            "tier": self.tier,
            "disposition": self.disposition,
            "reason": self.reason,
        }
        for key, value in (
            ("owner", self.owner),
            ("project_id", self.project_id),
            ("topic", self.topic),
        ):
            if value is not None:
                result[key] = value
        return result


def classify_inventory(
    inventory: Inventory,
    registry: RegistrySnapshot,
) -> tuple[LedgerEntry, ...]:
    """Classify every namespace into exactly one of the four reconciliation tiers."""
    entries = [
        *(_classify_qdrant(name, registry) for name in inventory.qdrant),
        *(_classify_falkor(name) for name in inventory.falkordb),
    ]
    return tuple(sorted(entries, key=lambda entry: (entry.store, entry.namespace)))


def _classify_qdrant(name: str, registry: RegistrySnapshot) -> LedgerEntry:
    if name in _QDRANT_RESERVED:
        return _entry("qdrant", name, 1, "keep", "reserved production collection")

    if name.startswith("code_symbols_"):
        project_id = name.removeprefix("code_symbols_")
        if name.startswith(_CODE_STANDALONE_PREFIX):
            return _entry(
                "qdrant",
                name,
                3,
                "delete",
                "proven standalone code-index harness collection",
                owner="gcode-drop-namespace",
            )
        if _is_uuid(project_id) and project_id in registry.project_ids:
            return _entry(
                "qdrant", name, 2, "keep", "registered project projection", project_id=project_id
            )
        if _is_uuid(project_id) and project_id in registry.code_indexed_project_ids:
            return _entry(
                "qdrant",
                name,
                3,
                "delete",
                "code-index registry orphaned from projects registry",
                owner="gcode-invalidate",
                project_id=project_id,
            )
        return _entry("qdrant", name, 4, "report", "unproven code-symbol namespace")

    if name.startswith("gwiki_project_"):
        project_id = name.removeprefix("gwiki_project_")
        if _is_uuid(project_id) and project_id in registry.project_ids:
            return _entry(
                "qdrant", name, 2, "keep", "registered project wiki", project_id=project_id
            )
        return _entry("qdrant", name, 4, "report", "unproven project wiki namespace")

    if name.startswith("gwiki_topic_"):
        topic = name.removeprefix("gwiki_topic_")
        if topic in registry.wiki_topics:
            return _entry("qdrant", name, 2, "keep", "registered topic wiki", topic=topic)
        return _entry(
            "qdrant",
            name,
            3,
            "delete",
            "topic projection absent from wiki registry",
            owner="gwiki-purge",
            topic=topic,
        )

    return _entry("qdrant", name, 4, "report", "unknown Qdrant collection")


def _classify_falkor(name: str) -> LedgerEntry:
    if name in _FALKOR_RESERVED:
        return _entry("falkordb", name, 1, "keep", "reserved production graph")
    if any(pattern.fullmatch(name) for pattern in _RECALL_GRAPH_PATTERNS):
        return _entry(
            "falkordb",
            name,
            3,
            "delete",
            "proven recall/debug harness graph",
            owner="recall-maintenance",
        )
    return _entry("falkordb", name, 4, "report", "unknown FalkorDB graph")


def _entry(
    store: str,
    namespace: str,
    tier: int,
    disposition: str,
    reason: str,
    *,
    owner: str | None = None,
    project_id: str | None = None,
    topic: str | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        store=store,
        namespace=namespace,
        tier=tier,
        disposition=disposition,
        reason=reason,
        owner=owner,
        project_id=project_id,
        topic=topic,
    )


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def build_candidate_manifest(
    inventory: Inventory,
    entries: list[LedgerEntry] | tuple[LedgerEntry, ...],
) -> dict[str, object]:
    ledger = [entry.to_dict() for entry in entries]
    return {
        "manifest_format": _CANDIDATE_FORMAT,
        "manifest_version": _MANIFEST_VERSION,
        "original_inventory": inventory.to_dict(),
        "ledger": ledger,
        "ledger_sha256": _canonical_sha256(ledger),
    }


def seal_deletion_manifest(
    candidate: Mapping[str, object],
    backup_manifest_sha256: str,
    backup_inventory: Mapping[str, list[str]],
) -> dict[str, object]:
    """Bind tier-3 deletions to both candidate ledger and verified backup inventory."""
    _validate_candidate(candidate)
    original = _inventory_from_object(candidate["original_inventory"])
    backed_up = _inventory_from_object(backup_inventory)
    if backed_up != original:
        raise RuntimeError("Fresh backup inventory does not exactly match candidate inventory")
    ledger = _ledger_objects(candidate["ledger"])
    deletions = [item for item in ledger if item.get("disposition") == "delete"]
    for item in deletions:
        store = _required_string(item, "store")
        namespace = _required_string(item, "namespace")
        if namespace not in getattr(backed_up, store):
            raise RuntimeError(
                f"Deletion target missing from backup inventory: {store}:{namespace}"
            )
    return {
        "manifest_format": _DELETION_FORMAT,
        "manifest_version": _MANIFEST_VERSION,
        "backup_manifest_sha256": backup_manifest_sha256,
        "ledger_sha256": candidate["ledger_sha256"],
        "original_inventory": original.to_dict(),
        "deletions": deletions,
    }


def _validate_candidate(candidate: Mapping[str, object]) -> None:
    if candidate.get("manifest_format") != _CANDIDATE_FORMAT:
        raise RuntimeError("Unexpected reconciliation candidate manifest format")
    if candidate.get("manifest_version") != _MANIFEST_VERSION:
        raise RuntimeError("Unexpected reconciliation candidate manifest version")
    ledger = _ledger_objects(candidate.get("ledger"))
    if candidate.get("ledger_sha256") != _canonical_sha256(ledger):
        raise RuntimeError("Candidate ledger sha256 mismatch")
    _inventory_from_object(candidate.get("original_inventory"))


def _ledger_objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError("Manifest ledger must be a list of objects")
    return [cast(dict[str, object], item) for item in value]


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _manifest_bytes(document: Mapping[str, object]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _write_manifest(path: Path, document: Mapping[str, object]) -> str:
    payload = _manifest_bytes(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"Refusing to overwrite different reconciliation manifest: {path}")
    else:
        path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def assert_resume_inventory(
    original: Inventory,
    current: Inventory,
    *,
    completed: set[tuple[str, str]],
) -> None:
    expected = Inventory(
        qdrant=original.qdrant - {name for store, name in completed if store == "qdrant"},
        falkordb=original.falkordb - {name for store, name in completed if store == "falkordb"},
    )
    missing = {
        *(("qdrant", name) for name in expected.qdrant - current.qdrant),
        *(("falkordb", name) for name in expected.falkordb - current.falkordb),
    }
    if missing:
        rendered = ", ".join(f"{store}:{name}" for store, name in sorted(missing))
        raise RuntimeError(f"Detected out-of-manifest deletion during resume: {rendered}")
    if current != expected:
        raise RuntimeError("Inventory drifted from original-inventory-minus-completed")


def owner_command(
    entry: LedgerEntry,
    *,
    manifest: Path,
    manifest_sha256: str,
) -> list[str]:
    if entry.owner == "gcode-invalidate" and entry.project_id is not None:
        return ["gcode", "invalidate", "--project-id", entry.project_id, "--force"]
    if entry.owner == "gcode-drop-namespace":
        return [
            "gcode",
            "drop-namespace",
            entry.namespace,
            "--manifest",
            str(manifest),
            "--manifest-sha256",
            manifest_sha256,
        ]
    if entry.owner == "gwiki-purge" and entry.topic is not None:
        return ["gwiki", "--topic", entry.topic, "purge", "--yes"]
    if entry.owner == "recall-maintenance":
        return [
            "gobby",
            "recall-maintenance",
            "drop-graph",
            entry.namespace,
            "--manifest",
            str(manifest),
            "--manifest-sha256",
            manifest_sha256,
        ]
    raise RuntimeError(f"No owner CLI for reconciliation target: {entry}")


@contextmanager
def _runtime_resources() -> Iterator[tuple[DaemonConfig, HubDatabase]]:
    owned_runtime: CliRuntime | None = None
    try:
        runtime = get_cli_runtime()
    except RuntimeError:
        owned_runtime = CliRuntime(config_file=None)
        runtime = owned_runtime
    try:
        yield (
            runtime.require_config(apply_migrations=False),
            runtime.require_database(apply_migrations=False),
        )
    finally:
        if owned_runtime is not None:
            owned_runtime.close()


def collect_live_state() -> tuple[Inventory, RegistrySnapshot]:
    """Collect read-only inventories and ownership registries."""
    with _runtime_resources() as (config, database):
        inventory = Inventory(
            qdrant=frozenset(_list_qdrant_collections(config)),
            falkordb=frozenset(_list_falkor_graphs(config)),
        )
        project_rows = database.fetchall("SELECT id FROM projects WHERE deleted_at IS NULL")
        indexed_rows = database.fetchall("SELECT id FROM code_indexed_projects")
    topics = _load_wiki_topics(
        required=any(name.startswith("gwiki_topic_") for name in inventory.qdrant)
    )
    return inventory, RegistrySnapshot(
        project_ids=frozenset(str(row["id"]) for row in project_rows),
        code_indexed_project_ids=frozenset(str(row["id"]) for row in indexed_rows),
        wiki_topics=frozenset(topics),
    )


def _list_qdrant_collections(config: DaemonConfig) -> list[str]:
    qdrant = config.databases.qdrant
    if not qdrant.url:
        raise RuntimeError("Qdrant URL is unavailable")
    client = QdrantClient(url=qdrant.url, api_key=qdrant.api_key, timeout=15)
    try:
        return sorted(collection.name for collection in client.get_collections().collections)
    finally:
        client.close()


def _list_falkor_graphs(config: DaemonConfig) -> list[str]:
    falkor = config.databases.falkordb
    client = cast(
        _RedisCommandClient,
        redis.Redis(
            host=falkor.host,
            port=falkor.port,
            password=falkor.password,
            decode_responses=True,
            socket_timeout=15,
        ),
    )
    try:
        result = client.execute_command("GRAPH.LIST")
        if not isinstance(result, list):
            raise RuntimeError("FalkorDB GRAPH.LIST returned an unexpected payload")
        return sorted(str(graph) for graph in result)
    finally:
        client.close()


def _load_wiki_topics(*, required: bool) -> set[str]:
    root = Path(os.environ.get("GOBBY_WIKI_HUB", str(Path.home() / "wiki")))
    registry_path = root / "wikis.json"
    if not registry_path.exists():
        if required:
            raise RuntimeError(
                f"Wiki registry is required for topic classification: {registry_path}"
            )
        return set()
    document = json.loads(registry_path.read_bytes())
    if not isinstance(document, dict) or not isinstance(document.get("topics"), dict):
        raise RuntimeError(f"Wiki registry has an unexpected shape: {registry_path}")
    topics = cast(dict[str, object], document["topics"])
    return set(topics)


def _inventory_from_object(value: object) -> Inventory:
    if not isinstance(value, Mapping):
        raise RuntimeError("Manifest inventory must be an object")
    qdrant = value.get("qdrant")
    falkordb = value.get("falkordb")
    if not isinstance(qdrant, list) or not all(isinstance(item, str) for item in qdrant):
        raise RuntimeError("Manifest Qdrant inventory must be a string list")
    if not isinstance(falkordb, list) or not all(isinstance(item, str) for item in falkordb):
        raise RuntimeError("Manifest FalkorDB inventory must be a string list")
    return Inventory(frozenset(qdrant), frozenset(falkordb))


def _required_string(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Manifest deletion has invalid {key}")
    return value


def _ledger_entry(item: Mapping[str, object]) -> LedgerEntry:
    tier = item.get("tier")
    disposition = item.get("disposition")
    reason = item.get("reason", "sealed deletion")
    if tier != 3 or disposition != "delete" or not isinstance(reason, str):
        raise RuntimeError("Deletion manifest contains a non-tier-3 target")
    return LedgerEntry(
        store=_required_string(item, "store"),
        namespace=_required_string(item, "namespace"),
        tier=3,
        disposition="delete",
        reason=reason,
        owner=cast(str | None, item.get("owner")),
        project_id=cast(str | None, item.get("project_id")),
        topic=cast(str | None, item.get("topic")),
    )


def _backup_inventory(batch: DestructiveBatch, epoch: MaintenanceEpoch) -> Inventory:
    if batch.backup_manifest_path is None or batch.backup_manifest_sha256 is None:
        raise RuntimeError("Reconciliation batch has no backup manifest binding")
    path = Path(batch.backup_manifest_path)
    payload = path.read_bytes()
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != batch.backup_manifest_sha256:
        raise RuntimeError("Recorded backup manifest sha256 does not match its file")
    manifest = load_manifest(path)
    if manifest.epoch_id != str(epoch.id):
        raise RuntimeError("Backup manifest belongs to a different maintenance epoch")
    for store_name in ("qdrant", "falkordb"):
        store = manifest.stores.get(store_name)
        if store is None or not store.restore_verified.verified:
            raise RuntimeError(f"Backup lacks restore verification for {store_name}")
    qdrant_collections = manifest.stores["qdrant"].details.get("collections")
    falkor_graphs = manifest.stores["falkordb"].details.get("graphs")
    if not isinstance(qdrant_collections, dict) or not isinstance(falkor_graphs, list):
        raise RuntimeError("Backup manifest lacks vector/graph inventory")
    return Inventory(
        qdrant=frozenset(str(name) for name in qdrant_collections),
        falkordb=frozenset(str(name) for name in falkor_graphs),
    )


class _ReconcileExecutor:
    def prepare_intent(self) -> dict[str, object]:
        inventory, registry = collect_live_state()
        candidate = build_candidate_manifest(inventory, classify_inventory(inventory, registry))
        directory = Path(
            os.environ.get(
                "GOBBY_RECONCILE_MANIFEST_DIR",
                str(get_gobby_home() / "maintenance" / "reconcile"),
            )
        )
        candidate_name = f"candidate-{candidate['ledger_sha256']}.json"
        candidate_path = directory / candidate_name
        candidate_sha256 = _write_manifest(candidate_path, candidate)
        click.echo(json.dumps(candidate["ledger"], indent=2, sort_keys=True))
        return {
            "campaign": "reconcile",
            "candidate_manifest": candidate,
            "candidate_manifest_path": str(candidate_path),
            "candidate_manifest_sha256": candidate_sha256,
        }

    def apply(self, epoch: MaintenanceEpoch, batch: DestructiveBatch) -> None:
        candidate, candidate_path = _candidate_from_batch(batch)
        backup = _backup_inventory(batch, epoch)
        backup_sha256 = cast(str, batch.backup_manifest_sha256)
        sealed = seal_deletion_manifest(candidate, backup_sha256, backup.to_dict())
        sealed_path = candidate_path.with_name(f"deletion-{backup_sha256}.json")
        sealed_sha256 = _write_manifest(sealed_path, sealed)
        entries = [_ledger_entry(item) for item in _ledger_objects(sealed["deletions"])]
        original = _inventory_from_object(sealed["original_inventory"])
        database_url = _resolve_database_url()

        for entry in entries:
            current_batch = get_destructive_batch(database_url, epoch.id, batch.id)
            if current_batch is None:
                raise RuntimeError(f"Destructive batch disappeared: {batch.id}")
            current, _registry = collect_live_state()
            completed = _completed_targets(current_batch, entries, current)
            assert_resume_inventory(original, current, completed=completed)
            target = f"{entry.store}:{entry.namespace}"
            command = owner_command(entry, manifest=sealed_path, manifest_sha256=sealed_sha256)

            def apply_owner(command: list[str] = command) -> None:
                _run_owner(command)

            def target_absent(entry: LedgerEntry = entry) -> bool:
                return not _target_exists(entry)

            run_receipted_component(
                database_url,
                epoch.id,
                batch.id,
                target=target,
                apply=apply_owner,
                postcondition=target_absent,
            )

    def verify(self, epoch: MaintenanceEpoch, batch: DestructiveBatch) -> None:
        candidate, _candidate_path = _candidate_from_batch(batch)
        backup = _backup_inventory(batch, epoch)
        sealed = seal_deletion_manifest(
            candidate,
            cast(str, batch.backup_manifest_sha256),
            backup.to_dict(),
        )
        entries = [_ledger_entry(item) for item in _ledger_objects(sealed["deletions"])]
        current_batch = get_destructive_batch(_resolve_database_url(), epoch.id, batch.id)
        if current_batch is None:
            raise RuntimeError(f"Destructive batch disappeared: {batch.id}")
        expected_targets = {f"{entry.store}:{entry.namespace}" for entry in entries}
        verified_targets = {
            target
            for target, receipt in current_batch.target_receipts.items()
            if receipt.get("state") == "verified"
        }
        if verified_targets & expected_targets != expected_targets:
            raise RuntimeError("Reconciliation target receipts are not all verified")
        current, _registry = collect_live_state()
        original = _inventory_from_object(sealed["original_inventory"])
        completed = {(entry.store, entry.namespace) for entry in entries}
        assert_resume_inventory(original, current, completed=completed)


def _candidate_from_batch(
    batch: DestructiveBatch,
) -> tuple[dict[str, object], Path]:
    raw_candidate = batch.intent.get("candidate_manifest")
    raw_path = batch.intent.get("candidate_manifest_path")
    expected_sha256 = batch.intent.get("candidate_manifest_sha256")
    if not isinstance(raw_candidate, dict) or not isinstance(raw_path, str):
        raise RuntimeError("Reconciliation batch lacks candidate manifest intent")
    if not isinstance(expected_sha256, str):
        raise RuntimeError("Reconciliation batch lacks candidate manifest sha256")
    candidate = cast(dict[str, object], raw_candidate)
    _validate_candidate(candidate)
    path = Path(raw_path)
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError("Candidate manifest file sha256 does not match batch intent")
    if json.loads(payload) != candidate:
        raise RuntimeError("Candidate manifest file differs from batch intent")
    return candidate, path


def _completed_targets(
    batch: DestructiveBatch,
    entries: list[LedgerEntry],
    current: Inventory,
) -> set[tuple[str, str]]:
    completed: set[tuple[str, str]] = set()
    for entry in entries:
        target = f"{entry.store}:{entry.namespace}"
        receipt = batch.target_receipts.get(target)
        if receipt is None:
            continue
        state = receipt.get("state")
        if state == "verified" or (
            state in {"pending", "applied"} and not _inventory_contains(current, entry)
        ):
            completed.add((entry.store, entry.namespace))
    return completed


def _target_exists(entry: LedgerEntry) -> bool:
    inventory, _registry = collect_live_state()
    return _inventory_contains(inventory, entry)


def _inventory_contains(inventory: Inventory, entry: LedgerEntry) -> bool:
    return entry.namespace in getattr(inventory, entry.store)


def _run_owner(command: list[str]) -> None:
    result = subprocess.run(  # nosec B603 - argv comes from sealed typed ledger fields
        command,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise RuntimeError(f"Owner CLI failed ({' '.join(command)}): {detail}")


@click.command()
@click.option("--dry-run", is_flag=True, required=True)
@click.option(
    "--manifest-output",
    type=click.Path(path_type=Path),
    required=True,
)
def main(dry_run: bool, manifest_output: Path) -> None:
    """Emit a keep/delete/report ledger and its hash-pinned candidate manifest."""
    if not dry_run:
        raise click.ClickException("Mutations run only through gobby hub-maintenance run reconcile")
    inventory, registry = collect_live_state()
    candidate = build_candidate_manifest(inventory, classify_inventory(inventory, registry))
    digest = _write_manifest(manifest_output, candidate)
    click.echo(json.dumps(candidate["ledger"], indent=2, sort_keys=True))
    click.echo(f"candidate_manifest={manifest_output}")
    click.echo(f"candidate_manifest_sha256={digest}")


register_campaign_executor("reconcile", _ReconcileExecutor())


if __name__ == "__main__":
    main()
