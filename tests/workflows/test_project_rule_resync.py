"""A landed project rule YAML edit refreshes its installed row on every reload.

The synthetic rule writes a reserved workflow variable. Each landed revision must
reach the project's installed row without operator intervention, including rows that
lost the sync tag before the fix, and a reload must never touch another project's row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.mcp_proxy.tools.workflows._import import reload_cache
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.pipeline_loader import PipelineLoader
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.integration

RULE_NAME = "synthetic-policy"
RULE_FILE = Path(".gobby") / "workflows" / "rules" / f"{RULE_NAME}.yaml"
SYNC_TAG = "user"
IMPORT_MARK = "project-workflow-import"
RULE_TAGS = ["default", RULE_NAME]


@pytest.fixture(autouse=True)
def isolated_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.workflows.imports.get_global_workflows_dir", lambda: tmp_path / "global"
    )

    def zero_sync(_db: object) -> dict[str, int]:
        return {"synced": 0, "updated": 0}

    monkeypatch.setattr("gobby.workflows.sync_pipelines.sync_bundled_pipelines", zero_sync)
    monkeypatch.setattr("gobby.workflows.sync_rules.sync_bundled_rules", zero_sync)
    monkeypatch.setattr("gobby.workflows.sync_variables.sync_bundled_variables", zero_sync)
    monkeypatch.setattr("gobby.agents.sync.sync_bundled_agents", zero_sync)


def _write_rule(root: Path, *, revision: str, rule_tags: list[str] | None) -> None:
    rule: dict[str, Any] = {
        "description": revision,
        "event": "turn_start",
        "effects": [
            {"type": "set_variable", "variable": "project_policy", "value": {"revision": revision}}
        ],
    }
    if rule_tags is not None:
        rule["tags"] = rule_tags
    path = root / RULE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"tags": ["seat"], "rules": {RULE_NAME: rule}}), encoding="utf-8"
    )


def _reload(db: HubDatabase, root: Path, project_id: str) -> None:
    result = reload_cache(
        PipelineLoader(db=db), db=db, project_path=str(root), project_id=project_id
    )
    assert "imported_workflow_sync_errors" not in result, result


def _landed_revision(row: Any) -> str:
    revision = row.definition_json["effects"][0]["value"]["revision"]
    assert isinstance(revision, str)
    return revision


@pytest.mark.parametrize(
    "rule_tags", [None, RULE_TAGS], ids=["file-tags", "rule-tags-override-file-tags"]
)
def test_repeated_landed_edits_refresh_the_project_row(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    rule_tags: list[str] | None,
) -> None:
    project_path = tmp_path / "resync-project"
    project = isolated_checkout_factory(temp_db, "resync-project", root=project_path).project
    manager = RuleDefinitionManager(temp_db)
    _write_rule(project_path, revision="revision 1", rule_tags=rule_tags)
    _reload(temp_db, project_path, project.id)
    row = manager.get_by_name(RULE_NAME, project_id=project.id)
    assert row is not None
    assert row.description == "revision 1"

    for revision in ("revision 2", "revision 3"):
        _write_rule(project_path, revision=revision, rule_tags=rule_tags)
        _reload(temp_db, project_path, project.id)

        current = manager.get(row.id)
        assert current.description == revision
        assert _landed_revision(current) == revision
        assert SYNC_TAG in (current.tags or [])


@pytest.mark.parametrize("marked", [True, False], ids=["with-import-marker", "without-marker"])
def test_row_that_lost_the_sync_tag_refreshes_on_next_reload(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    marked: bool,
) -> None:
    project_path = tmp_path / "legacy-project"
    project = isolated_checkout_factory(temp_db, "legacy-project", root=project_path).project
    manager = RuleDefinitionManager(temp_db)
    _write_rule(project_path, revision="revision 1", rule_tags=RULE_TAGS)
    _reload(temp_db, project_path, project.id)
    row = manager.get_by_name(RULE_NAME, project_id=project.id)
    assert row is not None

    # Rows installed before the fix kept the rule's own tags and lost the sync tag.
    legacy_tags = [
        tag for tag in row.tags or [] if tag != SYNC_TAG and (marked or tag != IMPORT_MARK)
    ]
    manager.update_from_sync(row.id, tags=legacy_tags)
    _write_rule(project_path, revision="revision 2", rule_tags=RULE_TAGS)
    _reload(temp_db, project_path, project.id)

    current = manager.get(row.id)
    assert current.description == "revision 2"
    assert _landed_revision(current) == "revision 2"


def test_other_projects_row_is_untouched_by_a_reload(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    project_a_path = tmp_path / "project-a"
    project_b_path = tmp_path / "project-b"
    project_a = isolated_checkout_factory(temp_db, "project-a", root=project_a_path).project
    project_b = isolated_checkout_factory(temp_db, "project-b", root=project_b_path).project
    manager = RuleDefinitionManager(temp_db)
    _write_rule(project_a_path, revision="a revision 1", rule_tags=RULE_TAGS)
    _write_rule(project_b_path, revision="b revision 1", rule_tags=RULE_TAGS)
    _reload(temp_db, project_a_path, project_a.id)
    _reload(temp_db, project_b_path, project_b.id)
    a_row = manager.get_by_name(RULE_NAME, project_id=project_a.id)
    b_row = manager.get_by_name(RULE_NAME, project_id=project_b.id)
    assert a_row is not None and b_row is not None

    # Project A's row has lost both ownership tags; project B's reload must leave it alone.
    manager.update_from_sync(
        a_row.id, tags=[tag for tag in a_row.tags or [] if tag not in (SYNC_TAG, IMPORT_MARK)]
    )
    before = manager.get(a_row.id)
    _write_rule(project_b_path, revision="b revision 2", rule_tags=RULE_TAGS)
    _reload(temp_db, project_b_path, project_b.id)

    after = manager.get(a_row.id)
    assert after.project_id == project_a.id
    assert after.description == "a revision 1"
    assert after.tags == before.tags
    refreshed = manager.get(b_row.id)
    assert refreshed.description == "b revision 2"
