"""Rule file imports can neither change, shadow nor author a protected rule."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.imports import sync_imported_workflow_file
from gobby.workflows.rule_authority import operator_rule_changes
from gobby.workflows.sync_rules import sync_bundled_rules, sync_rule_file

pytestmark = pytest.mark.unit

GATE = "synthetic-guarded-gate"


def _rule_file(path: Path, body: str, *, header: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{header}rules:\n  {GATE}:\n{body}")
    return path


def _gate(reason: str, *, extra: str = "") -> str:
    return (
        "    event: task_close\n"
        "    protected: true\n"
        "    effects:\n"
        "      - type: block\n"
        f"        reason: {reason}\n"
        f"{extra}"
    )


@pytest.fixture
def bundled(temp_db: HubDatabase, tmp_path: Path) -> Path:
    root = tmp_path / "bundled"
    _rule_file(root / "guards.yaml", _gate("First gate."), header='version: "1.0.0"\n')
    assert sync_bundled_rules(temp_db, root)["errors"] == []
    return root


def _row(db: HubDatabase, project_id: str | None = None) -> Any:
    row = RuleDefinitionManager(db).get_by_name(GATE, project_id=project_id)
    assert row is not None
    return row


def test_bundled_sync_updates_an_installed_protected_rule(
    temp_db: HubDatabase, bundled: Path
) -> None:
    _rule_file(bundled / "guards.yaml", _gate("Second gate."), header='version: "1.1.0"\n')

    result = sync_bundled_rules(temp_db, bundled)

    assert result["errors"] == []
    row = _row(temp_db)
    assert row.definition_json["protected"] is True
    assert row.definition_json["effects"][0]["reason"] == "Second gate."


@pytest.mark.parametrize("scope", ["user", "project"])
def test_rule_file_override_of_protected_rule_is_refused(
    temp_db: HubDatabase,
    bundled: Path,
    tmp_path: Path,
    sample_project: dict[str, Any],
    scope: str,
) -> None:
    project_id = str(sample_project["id"]) if scope == "project" else None
    path = _rule_file(
        tmp_path / scope / "override.yaml",
        "    event: task_close\n"
        "    override: true\n"
        "    enabled: false\n"
        "    effects:\n"
        "      - type: block\n"
        "        reason: Hijacked.\n",
    )

    result = sync_rule_file(temp_db, path, project_id=project_id)

    assert any("is protected" in error for error in result["errors"]), result
    effective = {row.name: row for row in RuleDefinitionManager(temp_db).list_effective(project_id)}
    assert effective[GATE].project_id is None
    assert effective[GATE].enabled is True
    assert effective[GATE].definition_json["effects"][0]["reason"] == "First gate."


def test_single_definition_override_of_protected_rule_is_refused(
    temp_db: HubDatabase, bundled: Path, tmp_path: Path, sample_project: dict[str, Any]
) -> None:
    project_id = str(sample_project["id"])
    path = tmp_path / "single.yaml"
    path.write_text(
        f"name: {GATE}\ntype: rule\noverride: true\nevent: task_close\nenabled: false\n"
        "effects:\n  - type: block\n    reason: Hijacked.\n"
    )

    with pytest.raises(ValueError, match="is protected"):
        sync_imported_workflow_file(temp_db, path, project_id)

    rows = RuleDefinitionManager(temp_db).list_all(project_id=project_id)
    assert [row.project_id for row in rows if row.name == GATE] == [None]


def test_rule_file_cannot_author_a_protected_rule(temp_db: HubDatabase, tmp_path: Path) -> None:
    path = _rule_file(tmp_path / "user" / "new.yaml", _gate("Self-protected."))

    refused = sync_rule_file(temp_db, path)
    assert any("cannot be marked protected" in error for error in refused["errors"]), refused
    assert RuleDefinitionManager(temp_db).get_by_name(GATE) is None

    with operator_rule_changes():
        allowed = sync_rule_file(temp_db, path)
    assert allowed["errors"] == []
    assert _row(temp_db).definition_json["protected"] is True


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
def test_project_row_displaces_only_an_unprotected_global_row(
    temp_db: HubDatabase, sample_project: dict[str, Any], protected: bool
) -> None:
    project_id = str(sample_project["id"])
    manager = RuleDefinitionManager(temp_db)
    body: dict[str, Any] = {"event": "task_close", "effects": [{"type": "block", "reason": "g"}]}
    manager.create(GATE, {**body, "protected": protected}, source="installed")
    manager.create(GATE, body, project_id=project_id, source="installed")

    (effective,) = [row for row in manager.list_effective(project_id) if row.name == GATE]

    assert effective.project_id == (None if protected else project_id)
