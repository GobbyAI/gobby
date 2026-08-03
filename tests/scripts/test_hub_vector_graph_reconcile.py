from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from click.testing import CliRunner

import scripts.hub_vector_graph_reconcile as reconcile_mod
from gobby.storage.maintenance_epoch import DestructiveBatch
from scripts.hub_vector_graph_reconcile import (
    Inventory,
    LedgerEntry,
    RegistrySnapshot,
    assert_resume_inventory,
    build_candidate_manifest,
    classify_inventory,
    main,
    owner_command,
    seal_deletion_manifest,
)

PROBE_PROJECT_ID = "28888a1a-8ed3-44b8-8a73-237c4b74a548"
LIVE_PROJECT_ID = "019bfef8-89bb-7bd1-a5c3-80baabdff01b"


def _classified() -> tuple[Inventory, list[LedgerEntry]]:
    inventory = Inventory(
        qdrant=frozenset(
            {
                "memories",
                f"code_symbols_{LIVE_PROJECT_ID}",
                f"code_symbols_{PROBE_PROJECT_ID}",
                "code_symbols_graph-standalone-123",
                "gwiki_topic_registered",
                "gwiki_topic_scratch-run",
                "unknown_vectors",
            }
        ),
        falkordb=frozenset(
            {
                "gobby_code",
                "test_recall_benchmark_123",
                "probe_cluster_123",
                "customer_graph",
            }
        ),
    )
    registry = RegistrySnapshot(
        project_ids=frozenset({LIVE_PROJECT_ID}),
        code_indexed_project_ids=frozenset({LIVE_PROJECT_ID, PROBE_PROJECT_ID}),
        wiki_topics=frozenset({"registered"}),
    )
    return inventory, list(classify_inventory(inventory, registry))


def test_classification_is_strict_four_tier_and_keeps_unknowns_report_only() -> None:
    _inventory, entries = _classified()
    ledger = {(entry.store, entry.namespace): entry for entry in entries}

    assert (ledger[("qdrant", "memories")].tier, ledger[("qdrant", "memories")].disposition) == (
        1,
        "keep",
    )
    live = ledger[("qdrant", f"code_symbols_{LIVE_PROJECT_ID}")]
    assert (live.tier, live.disposition) == (2, "keep")
    probe = ledger[("qdrant", f"code_symbols_{PROBE_PROJECT_ID}")]
    assert (probe.tier, probe.disposition, probe.owner, probe.project_id) == (
        3,
        "delete",
        "gcode-invalidate",
        PROBE_PROJECT_ID,
    )
    standalone = ledger[("qdrant", "code_symbols_graph-standalone-123")]
    assert (standalone.tier, standalone.owner) == (3, "gcode-drop-namespace")
    assert ledger[("qdrant", "gwiki_topic_registered")].tier == 2
    scratch = ledger[("qdrant", "gwiki_topic_scratch-run")]
    assert (scratch.tier, scratch.owner, scratch.topic) == (3, "gwiki-purge", "scratch-run")
    recall = ledger[("falkordb", "test_recall_benchmark_123")]
    assert (recall.tier, recall.owner) == (3, "recall-maintenance")
    for target in (("qdrant", "unknown_vectors"), ("falkordb", "customer_graph")):
        assert (ledger[target].tier, ledger[target].disposition) == (4, "report")


def test_candidate_and_deletion_manifests_are_hash_pinned_to_ledger_and_backup() -> None:
    inventory, entries = _classified()
    candidate = build_candidate_manifest(inventory, entries)
    backup_sha256 = "a" * 64
    backup_inventory = {
        "qdrant": sorted(inventory.qdrant),
        "falkordb": sorted(inventory.falkordb),
    }

    sealed = seal_deletion_manifest(candidate, backup_sha256, backup_inventory)

    expected_ledger_hash = hashlib.sha256(
        json.dumps(candidate["ledger"], separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert candidate["ledger_sha256"] == expected_ledger_hash
    assert sealed["ledger_sha256"] == expected_ledger_hash
    assert sealed["backup_manifest_sha256"] == backup_sha256
    deletions = cast(list[dict[str, object]], sealed["deletions"])
    assert {item["namespace"] for item in deletions} == {
        f"code_symbols_{PROBE_PROJECT_ID}",
        "code_symbols_graph-standalone-123",
        "gwiki_topic_scratch-run",
        "test_recall_benchmark_123",
        "probe_cluster_123",
    }


def test_sealing_refuses_candidate_missing_from_fresh_backup_inventory() -> None:
    inventory, entries = _classified()
    candidate = build_candidate_manifest(inventory, entries)

    with pytest.raises(RuntimeError, match="backup inventory"):
        seal_deletion_manifest(
            candidate,
            "a" * 64,
            {"qdrant": [], "falkordb": sorted(inventory.falkordb)},
        )


def test_resume_hard_fails_on_any_out_of_manifest_deletion() -> None:
    original = Inventory(qdrant=frozenset({"keep", "delete-me"}), falkordb=frozenset())
    current = Inventory(qdrant=frozenset(), falkordb=frozenset())

    with pytest.raises(RuntimeError, match="out-of-manifest deletion"):
        assert_resume_inventory(
            original,
            current,
            completed={("qdrant", "delete-me")},
        )


def test_resume_reapplies_an_applied_receipt_when_target_still_exists() -> None:
    entry = LedgerEntry("qdrant", "delete-me", 3, "delete", "test")
    batch = cast(
        DestructiveBatch,
        SimpleNamespace(
            target_receipts={"qdrant:delete-me": {"state": "applied"}},
        ),
    )
    current = Inventory(qdrant=frozenset({"delete-me"}), falkordb=frozenset())

    completed = reconcile_mod._completed_targets(batch, [entry], current)

    assert completed == set()


def test_new_owner_commands_receive_exact_hash_pinned_manifest(tmp_path: Path) -> None:
    _inventory, entries = _classified()
    manifest = tmp_path / "sealed.json"
    manifest.write_text("{}")
    digest = "c" * 64
    commands = {
        entry.owner: owner_command(entry, manifest=manifest, manifest_sha256=digest)
        for entry in entries
        if entry.disposition == "delete"
    }

    for owner in ("gcode-drop-namespace", "recall-maintenance"):
        command = commands[owner]
        assert str(manifest) in command
        assert digest in command
    assert commands["gcode-invalidate"] == [
        "gcode",
        "invalidate",
        "--project-id",
        PROBE_PROJECT_ID,
        "--force",
    ]
    assert commands["gwiki-purge"] == ["gwiki", "--topic", "scratch-run", "purge", "--yes"]


def test_dry_run_emits_keep_delete_report_ledger_and_writes_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, entries = _classified()
    registry = RegistrySnapshot(
        project_ids=frozenset({LIVE_PROJECT_ID}),
        code_indexed_project_ids=frozenset({LIVE_PROJECT_ID, PROBE_PROJECT_ID}),
        wiki_topics=frozenset({"registered"}),
    )
    monkeypatch.setattr(reconcile_mod, "collect_live_state", lambda: (inventory, registry))
    manifest = tmp_path / "candidate.json"

    result = CliRunner().invoke(
        main,
        ["--dry-run", "--manifest-output", str(manifest)],
    )

    assert result.exit_code == 0, result.output
    assert all(disposition in result.output for disposition in ('"keep"', '"delete"', '"report"'))
    document = json.loads(manifest.read_bytes())
    assert document == build_candidate_manifest(inventory, entries)
