"""Pin the eight domain tables against the applied-schema catalog."""

from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CATALOG = _REPO_ROOT / "crates/gcore/assets/schema/catalog.manifest.json"
_BASELINE = _REPO_ROOT / "crates/gcore/assets/schema/baseline.sql"

DOMAIN_TABLES: tuple[str, ...] = (
    "agent_definitions",
    "agent_step_instances",
    "agent_step_workflows",
    "definition_revisions",
    "legacy_copy_ledger",
    "pipeline_definitions",
    "rule_definitions",
    "session_variable_defaults",
)
DEFINITION_TABLES: tuple[str, ...] = (
    "agent_definitions",
    "pipeline_definitions",
    "rule_definitions",
    "session_variable_defaults",
)
LEGACY_TABLES: tuple[str, ...] = ("workflow_definitions", "workflow_instances")


def _catalog_entries(kind: str) -> list[dict[str, str]]:
    payload: object = json.loads(_CATALOG.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    raw = payload[kind]
    assert isinstance(raw, list)
    entries: list[dict[str, str]] = []
    for item in raw:
        assert isinstance(item, dict)
        name = item["name"]
        definition = item["definition"]
        assert isinstance(name, str)
        assert isinstance(definition, str)
        entries.append({"name": name, "definition": definition})
    return entries


def _column_names() -> set[str]:
    return {entry["name"] for entry in _catalog_entries("columns")}


def _table_names(column_names: set[str]) -> set[str]:
    return {name.split(".", 1)[0] for name in column_names}


def test_baseline_keeps_legacy_tables_and_declares_domain_tables() -> None:
    baseline = _BASELINE.read_text(encoding="utf-8")
    for table in DOMAIN_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in baseline
    for table in LEGACY_TABLES:
        assert f"CREATE TABLE {table} (" in baseline
    for table in DEFINITION_TABLES:
        assert (
            f"ON {table} USING btree (name, project_id) NULLS NOT DISTINCT "
            "WHERE (deleted_at IS NULL)"
        ) in baseline


def test_catalog_pins_domain_tables_reconciliation_and_live_name_indexes() -> None:
    column_names = _column_names()
    tables = _table_names(column_names)
    indexes = [entry["definition"] for entry in _catalog_entries("indexes")]

    assert tables.issuperset(DOMAIN_TABLES)
    assert tables.issuperset(LEGACY_TABLES)

    for table in DEFINITION_TABLES:
        assert f"{table}.enabled" in column_names
        assert f"{table}.enabled_pinned" in column_names

    workflow_columns = {name for name in column_names if name.startswith("agent_step_workflows.")}
    assert "agent_step_workflows.enabled" not in workflow_columns
    assert "agent_step_workflows.enabled_pinned" not in workflow_columns

    for table in DEFINITION_TABLES:
        live_name = [
            definition
            for definition in indexes
            if f"$schema.{table} USING btree (name, project_id) NULLS NOT DISTINCT" in definition
        ]
        assert live_name, f"missing live-name unique index for {table}"
        assert all("CREATE UNIQUE INDEX" in definition for definition in live_name)
        assert all("WHERE (deleted_at IS NULL)" in definition for definition in live_name)
