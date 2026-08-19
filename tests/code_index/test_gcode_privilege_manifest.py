"""Contract tests for the managed gcode PostgreSQL privilege inventory."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "crates/gcode/security/managed_postgres_privileges.json"
RUST_ROOTS = (ROOT / "crates/gcode/src", ROOT / "crates/gcore/src")
CALL_RE = re.compile(
    r"\.(query|query_one|query_opt|execute|batch_execute|prepare|prepare_cached)\s*\("
)


def _production_prefix(source: str) -> str:
    return source.split("#[cfg(test)]", 1)[0]


def _postgres_call_inventory() -> dict[str, dict[str, int]]:
    inventory: dict[str, dict[str, int]] = {}
    for root in RUST_ROOTS:
        for path in sorted(root.rglob("*.rs")):
            relative = path.relative_to(ROOT).as_posix()
            if "/tests/" in relative or path.name == "tests.rs" or path.name.endswith("_tests.rs"):
                continue
            counts = Counter(CALL_RE.findall(_production_prefix(path.read_text())))
            if counts:
                inventory[relative] = dict(sorted(counts.items()))
    return inventory


def _load_manifest() -> dict[str, Any]:
    assert MANIFEST.exists(), "managed gcode privilege manifest is required"
    return cast(dict[str, Any], json.loads(MANIFEST.read_text()))


def test_manifest_covers_every_rust_database_call_at_head() -> None:
    manifest = _load_manifest()
    recorded = {entry["path"]: entry["calls"] for entry in manifest["source_inventory"]}
    assert recorded == _postgres_call_inventory()
    assert all(
        entry["classification"]
        in {"project-read", "project-write", "operator-only", "external-non-postgresql"}
        for entry in manifest["source_inventory"]
    )


def test_manifest_privileges_match_the_managed_relation_set() -> None:
    manifest = _load_manifest()
    assert manifest["version"] == 1
    assert manifest["principal"] == "gobby_gcode_capability"
    relations = {entry["relation"]: entry for entry in manifest["relations"]}
    assert set(relations) == {
        "projects",
        "config_state",
        "code_indexed_projects",
        "code_indexed_project_states",
        "code_indexed_file_states",
        "code_indexed_files",
        "code_symbols",
        "code_imports",
        "code_calls",
        "code_inheritance",
        "code_content_chunks",
        "code_index_projection_cleanup_pending",
        "code_index_prune_dirty_projects",
    }
    assert relations["projects"]["operations"] == ["SELECT"]
    assert relations["config_state"] == {
        "relation": "config_state",
        "operations": ["SELECT"],
        "columns": ["id", "revision"],
        "scope_note": "single global runtime configuration revision row; no per-project data",
    }
    assert relations["code_symbols"]["operations"] == [
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
    ]
    assert relations["code_inheritance"]["operations"] == [
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
    ]
    assert relations["code_inheritance"]["scope_column"] == "project_id"
    sequences = {entry["sequence"]: entry["operations"] for entry in manifest["sequences"]}
    assert sequences == {
        "code_imports_id_seq": ["USAGE", "SELECT"],
        "code_calls_id_seq": ["USAGE", "SELECT"],
        "code_inheritance_id_seq": ["USAGE", "SELECT"],
    }
    assert "config_store" in manifest["explicitly_denied_relations"]
    assert "tasks" in manifest["explicitly_denied_relations"]
    assert "sessions" in manifest["explicitly_denied_relations"]
    assert "schema_migrations" in manifest["explicitly_denied_relations"]
    inventory = {entry["path"]: entry for entry in manifest["source_inventory"]}
    assert inventory["crates/gcode/src/config/runtime_contract.rs"]["classification"] == (
        "project-read"
    )
