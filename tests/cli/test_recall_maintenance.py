from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gobby.cli.recall_maintenance import recall_maintenance

recall_maintenance_mod = importlib.import_module("gobby.cli.recall_maintenance")


def _write_manifest(tmp_path: Path, graph: str) -> tuple[Path, str]:
    manifest = {
        "manifest_format": "gobby-vector-graph-reconcile-deletion",
        "manifest_version": 1,
        "backup_manifest_sha256": "a" * 64,
        "ledger_sha256": "b" * 64,
        "original_inventory": {"qdrant": [], "falkordb": [graph]},
        "deletions": [
            {
                "store": "falkordb",
                "namespace": graph,
                "tier": 3,
                "disposition": "delete",
                "owner": "recall-maintenance",
            }
        ],
    }
    path = tmp_path / "deletions.json"
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode()
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    "graph",
    [
        "test_recall_benchmark_123",
        "test_recall_benchmark_e2e_123",
        "dbg17_fixture",
        "probe_cluster_456",
    ],
)
def test_drop_graph_accepts_only_owned_exact_manifest_targets(
    tmp_path: Path,
    graph: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, digest = _write_manifest(tmp_path, graph)
    deleted: list[str] = []
    monkeypatch.setattr(recall_maintenance_mod, "_list_graphs", lambda: [graph, "gobby_kg"])
    monkeypatch.setattr(recall_maintenance_mod, "_delete_graph", deleted.append)

    result = CliRunner().invoke(
        recall_maintenance,
        [
            "drop-graph",
            graph,
            "--manifest",
            str(manifest),
            "--manifest-sha256",
            digest,
        ],
    )

    assert result.exit_code == 0, result.output
    assert deleted == [graph]


def test_drop_graph_rejects_manifest_hash_mismatch(tmp_path: Path) -> None:
    graph = "probe_cluster_456"
    manifest, _digest = _write_manifest(tmp_path, graph)

    result = CliRunner().invoke(
        recall_maintenance,
        [
            "drop-graph",
            graph,
            "--manifest",
            str(manifest),
            "--manifest-sha256",
            "0" * 64,
        ],
    )

    assert result.exit_code != 0
    assert "sha256" in result.output.lower()


@pytest.mark.parametrize("graph", ["gobby_code", "gobby_wiki", "gobby_kg", "gwiki"])
def test_drop_graph_deny_lists_reserved_graphs(tmp_path: Path, graph: str) -> None:
    manifest, digest = _write_manifest(tmp_path, graph)

    result = CliRunner().invoke(
        recall_maintenance,
        [
            "drop-graph",
            graph,
            "--manifest",
            str(manifest),
            "--manifest-sha256",
            digest,
        ],
    )

    assert result.exit_code != 0
    assert "reserved" in result.output.lower()


def test_drop_graph_rejects_out_of_scope_name_even_when_manifest_lists_it(tmp_path: Path) -> None:
    graph = "customer_graph"
    manifest, digest = _write_manifest(tmp_path, graph)

    result = CliRunner().invoke(
        recall_maintenance,
        [
            "drop-graph",
            graph,
            "--manifest",
            str(manifest),
            "--manifest-sha256",
            digest,
        ],
    )

    assert result.exit_code != 0
    assert "owned" in result.output.lower()


def test_drop_graph_rejects_target_missing_from_original_inventory(tmp_path: Path) -> None:
    graph = "probe_cluster_456"
    manifest, _digest = _write_manifest(tmp_path, graph)
    document = json.loads(manifest.read_bytes())
    document["original_inventory"]["falkordb"] = []
    payload = json.dumps(document, indent=2, sort_keys=True).encode()
    manifest.write_bytes(payload)

    result = CliRunner().invoke(
        recall_maintenance,
        [
            "drop-graph",
            graph,
            "--manifest",
            str(manifest),
            "--manifest-sha256",
            hashlib.sha256(payload).hexdigest(),
        ],
    )

    assert result.exit_code != 0
    assert "original" in result.output.lower()
