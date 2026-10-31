"""Direct integration authority is declared, path-bounded and name-independent."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.tasks.test_close_invariants import git
from tests.tasks.test_step_records import _Seats

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("definition_name", "capability", "globs", "paths", "expected"),
    [
        ("custom-config-editor", True, ["policy/*.yaml"], ["policy/a.yaml"], "allow"),
        ("another-editor", True, ["policy/*.yaml"], ["policy/a.yaml"], "allow"),
        ("orchestrator", False, ["policy/*.yaml"], ["policy/a.yaml"], "block"),
        ("unprivileged-editor", False, ["policy/*.yaml"], ["policy/a.yaml"], "block"),
        ("empty-grant", True, [], ["policy/a.yaml"], "block"),
        (
            "mixed-editor",
            True,
            ["policy/*.yaml"],
            ["policy/a.yaml", "src/a.py"],
            "block",
        ),
        ("markdown-editor", False, [], ["docs/audit.md"], "block"),
    ],
)
async def test_direct_integration_rule_requires_capability_and_every_path(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
    definition_name: str,
    capability: bool,
    globs: list[str],
    paths: list[str],
    expected: str,
) -> None:
    bundled = get_bundled_rules_path()
    rules: list[str] = []
    for file in bundled.rglob("*.yaml"):
        content = file.read_text()
        if "direct_integration_commit" in content:
            rules.extend(yaml.safe_load(content).get("rules", {}))
    assert rules, "Direct integration authorization must be declared in bundled rule YAML."
    sync_bundled_rules(temp_db, bundled)
    temp_db.execute("DELETE FROM rule_definitions WHERE NOT (name = ANY(%s))", (rules,))
    repo = Path(sample_git_project["repo_path"])
    git(repo, "branch", "-m", "0.5.0")
    for name in paths:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed\n")
    git(repo, "add", "--", *paths)
    session_id = _Seats(temp_db, sample_git_project["id"]).add(
        definition_name,
        [],
        capabilities=("direct_integration_commit",) if capability else (),
        definition_fields={"direct_integration_commit_paths": globs},
    )
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        project_id=sample_git_project["id"],
        cwd=str(repo),
        data={
            "tool_name": "Bash",
            "command": "git commit -m 'configuration update'",
            "tool_input": {"command": "git commit -m 'configuration update'"},
            "staged_paths": paths,
            "integration_branch": "0.5.0",
        },
    )

    response = await RuleEngine(temp_db).evaluate(
        event, session_id=session_id, variables={"_agent_type": definition_name}
    )

    assert response.decision == expected, response.reason
    if expected == "block":
        assert "land_commit" in (response.reason or "")
