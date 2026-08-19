"""Tests for retired bundled workflow and agent definitions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gobby.agents.sync import sync_bundled_agents
from gobby.skills.sync import sync_bundled_skills
from gobby.storage.definitions import AgentDefinitionManager, PipelineDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager
from gobby.workflows.sync_pipelines import sync_bundled_pipelines

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / "src/gobby/install/shared/workflows"
PIPELINES_DIR = WORKFLOWS_DIR / "pipelines"
AGENTS_DIR = WORKFLOWS_DIR / "agents"
RULES_DIR = WORKFLOWS_DIR / "rules"
VARIABLES_PATH = WORKFLOWS_DIR / "variables/gobby-default-variables.yaml"
PROMPTS_DIR = REPO_ROOT / "src/gobby/install/shared/prompts"
SKILLS_DIR = REPO_ROOT / "src/gobby/install/shared/skills"
DOCS_DIR = REPO_ROOT / "docs/guides"
RESEARCH_CONTRACT = REPO_ROOT / "docs/contracts/gwiki-research.md"
RETIRED_RESEARCH_GUIDE = REPO_ROOT / "docs/guides/wiki-research.md"

RETIRED_PIPELINES = (
    "orchestrator",
    "front-half-orchestrator",
    "dev-orchestrator",
    "delivery-orchestrator",
    "dev",
    "merge-clone",
    "merge-worktree",
    "nightly-fixes",
    "qa",
    "spawn-developer",
    "spawn-qa",
    "wiki-research",
)
RETIRED_AGENTS = (
    "developer",
    "pipeline-worker",
    "nightly-linter",
    "nightly-test-fixer",
    "wiki-researcher",
)
RETIRED_SKILLS = ("dev", "qa", "wiki-research")
RETIRED_RULES = {
    "block-and-teach-context7",
    "block-writes-outside-plan-artifact",
    "no-npx",
    "require-memory-review-before-status",
}
MONOLITH_RULES = {
    "require-decompose-monolith-before-threshold-write",
    "require-monolith-resolution-before-commit",
    "require-monolith-resolution-before-task-transition",
    "require-monolith-resolution-before-turn-end",
}


def test_no_external_conductor_imports_remain() -> None:
    module = "gobby" + ".conductor"
    matches: list[str] = []

    for base in (REPO_ROOT / "src", REPO_ROOT / "tests"):
        for path in base.rglob("*.py"):
            text = path.read_text()
            if f"from {module}" in text or f"import {module}" in text:
                matches.append(str(path.relative_to(REPO_ROOT)))

    assert matches == []


@pytest.mark.parametrize("name", RETIRED_PIPELINES)
def test_retired_pipeline_yaml_is_absent_from_active_and_deprecated_bundles(
    name: str,
) -> None:
    candidates = (
        WORKFLOWS_DIR / f"{name}.yaml",
        PIPELINES_DIR / f"{name}.yaml",
        PIPELINES_DIR / "deprecated" / f"{name}.yaml",
    )

    assert not any(path.exists() for path in candidates), (
        f"retired pipeline remains bundled: {[path for path in candidates if path.exists()]}"
    )


@pytest.mark.parametrize("name", RETIRED_AGENTS)
def test_retired_agent_yaml_is_absent_from_active_and_deprecated_bundles(name: str) -> None:
    active_path = AGENTS_DIR / f"{name}.yaml"
    deprecated_path = AGENTS_DIR / "deprecated" / f"{name}.yaml"

    assert not active_path.exists(), f"retired agent remains active: {active_path}"
    assert not deprecated_path.exists(), f"retired agent tombstone remains: {deprecated_path}"


@pytest.mark.parametrize("name", RETIRED_SKILLS)
def test_retired_skill_is_absent_from_bundled_templates(name: str) -> None:
    assert not (SKILLS_DIR / name).exists(), f"retired skill remains bundled: {name}"


def test_retired_wiki_research_dispatch_is_not_advertised() -> None:
    contract = RESEARCH_CONTRACT.read_text(encoding="utf-8")

    assert "does not bundle a\n`wiki-research` pipeline" in contract
    assert "gobby pipelines run wiki-research" not in contract
    assert "pipeline:wiki-research" not in contract
    assert not RETIRED_RESEARCH_GUIDE.exists()


def test_retired_rules_are_absent_from_bundled_templates() -> None:
    bundled_rule_names: set[str] = set()
    for path in RULES_DIR.rglob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("rules"), dict):
            bundled_rule_names.update(data["rules"])

    assert RETIRED_RULES.isdisjoint(bundled_rule_names)


def test_interactive_destructive_git_guard_is_restored() -> None:
    path = RULES_DIR / "worker-safety/no-destructive-git-interactive.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rule = data["rules"]["no-destructive-git-interactive"]
    autonomous_path = RULES_DIR / "worker-safety/no-destructive-git.yaml"
    autonomous_data = yaml.safe_load(autonomous_path.read_text(encoding="utf-8"))
    autonomous_rule = autonomous_data["rules"]["no-destructive-git"]

    assert rule["enabled"] is True
    assert rule["when"] == "not variables.get('is_spawned_agent')"
    pattern = rule["effects"][0]["command_pattern"]
    assert pattern == autonomous_rule["effects"][0]["command_pattern"]
    for command in (
        "git reset --hard",
        "git clean -fdx",
        "git checkout .",
        "git restore .",
        "git branch -D obsolete",
        "git -C /repo reset --hard",
        "git -c core.hooksPath=/tmp checkout .",
        "git --git-dir=/repo/.git clean -fdx",
        "git --work-tree /repo restore .",
        "git --no-pager branch -D obsolete",
    ):
        assert re.search(pattern, command), command
    assert re.search(pattern, "git reset HEAD~1") is None


def test_planner_enables_surviving_plan_mode_write_guard() -> None:
    planner = yaml.safe_load((AGENTS_DIR / "planner.yaml").read_text(encoding="utf-8"))
    reset = yaml.safe_load(
        (RULES_DIR / "plan-mode/reset-plan-mode-on-session-start.yaml").read_text(encoding="utf-8")
    )["rules"]["reset-plan-mode-on-session-start"]
    guard = yaml.safe_load(
        (RULES_DIR / "plan-mode/block-edits-plan-mode.yaml").read_text(encoding="utf-8")
    )["rules"]["block-edits-plan-mode"]

    assert planner["workflows"]["variables"]["plan_mode"] is True
    assert "variables.get('_agent_type') != 'planner'" in reset["when"]
    assert "variables.get('plan_mode')" in guard["when"]
    assert guard["effects"][0]["tools"] == ["Edit", "Write", "NotebookEdit"]


def test_retired_memory_review_gate_has_no_live_state_or_guidance() -> None:
    current_surfaces = [
        *WORKFLOWS_DIR.rglob("*.yaml"),
        SKILLS_DIR / "live-session/SKILL.md",
        SKILLS_DIR / "tasks/SKILL.md",
        DOCS_DIR / "memory.md",
    ]

    for path in current_surfaces:
        assert "memory_review_completed" not in path.read_text(encoding="utf-8"), path


def test_retired_context7_rule_has_no_orphaned_session_state() -> None:
    variables = yaml.safe_load(VARIABLES_PATH.read_text(encoding="utf-8"))["variables"]
    reset = yaml.safe_load(
        (RULES_DIR / "skill-discovery/reset-skill-injection.yaml").read_text(encoding="utf-8")
    )["rules"]["reset-skill-injection"]
    reset_variables = {
        effect.get("variable") for effect in reset["effects"] if effect["type"] == "set_variable"
    }

    assert "context7_nudge_fired" not in variables
    assert "context7_available" not in variables
    assert "context7_nudge_fired" not in reset_variables
    assert "context7_available" not in (DOCS_DIR / "variables.md").read_text(encoding="utf-8")


def test_monolith_rule_templates_match_enabled_db_authority() -> None:
    path = RULES_DIR / "monolith-enforcement/require-same-session-decomposition.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert set(data["rules"]) == MONOLITH_RULES
    assert all(rule["enabled"] is True for rule in data["rules"].values())


def test_bundled_agents_have_no_dead_sync_selector() -> None:
    offenders: list[str] = []
    for path in AGENTS_DIR.glob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        excludes = data.get("workflows", {}).get("rule_selectors", {}).get("exclude", [])
        if "tag:sync" in excludes:
            offenders.append(path.name)

    assert offenders == []


def test_retired_digest_prompt_is_absent() -> None:
    assert not (PROMPTS_DIR / "memory/digest_update.md").exists()


def test_removed_bundled_pipeline_sync_soft_deletes_installed_row(
    tmp_path: Path, temp_db: HubDatabase
) -> None:
    db = temp_db
    manager = PipelineDefinitionManager(db)
    manager.create(
        name="orchestrator",
        definition_json={
            "name": "orchestrator",
            "type": "pipeline",
            "description": "old definition",
            "steps": [{"id": "noop", "exec": "true"}],
        },
        source="installed",
        tags=["gobby"],
        enabled=True,
    )

    pipelines_dir = tmp_path / "pipelines"
    pipelines_dir.mkdir()
    (pipelines_dir / "retained.yaml").write_text(
        """
name: retained
type: pipeline
description: retained definition
enabled: false
steps:
  - id: noop
    exec: "true"
""",
        encoding="utf-8",
    )

    with patch(
        "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pipelines_dir
    ):
        result = sync_bundled_pipelines(db)

    assert result["errors"] == []
    assert result["orphaned"] == 1

    assert manager.get_by_name("orchestrator") is None
    row = manager.get_by_name("orchestrator", include_deleted=True)
    assert row is not None
    assert row.deleted_at is not None
    assert row.enabled is True
    assert "deprecated" not in json.dumps(row.definition_json)


@pytest.mark.parametrize("name", RETIRED_AGENTS)
def test_removed_bundled_agent_sync_soft_deletes_installed_row(
    name: str, tmp_path: Path, temp_db: HubDatabase
) -> None:
    db = temp_db
    manager = AgentDefinitionManager(db)
    manager.create(
        name=name,
        definition_json=json.dumps(
            {
                "name": name,
                "description": "old definition",
                "enabled": True,
            }
        ),
        source="installed",
        tags=["gobby"],
        enabled=True,
    )

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
        result = sync_bundled_agents(db)

    assert result["errors"] == []
    assert result["orphaned"] == 1

    assert manager.get_by_name(name) is None
    row = manager.get_by_name(name, include_deleted=True)
    assert row is not None
    assert row.deleted_at is not None
    assert row.enabled is True
    definition = row.definition_json
    assert definition == {
        "name": name,
        "description": "old definition",
        "enabled": True,
    }


@pytest.mark.parametrize("name", RETIRED_SKILLS)
def test_removed_bundled_skill_sync_soft_deletes_installed_row(
    name: str, temp_db: HubDatabase
) -> None:
    manager = LocalSkillManager(temp_db)
    manager.create_skill(
        name=name,
        description="old bundled launcher",
        content=f"# {name}\nOld bundled launcher content.",
        metadata={"gobby": {"audience": "all"}},
        source="installed",
        source_type="filesystem",
    )

    result = sync_bundled_skills(temp_db)

    assert result["errors"] == []
    assert result["orphaned"] == 1
    assert manager.get_by_name(name) is None
    row = manager.get_by_name(name, include_deleted=True)
    assert row is not None
    assert row.deleted_at is not None
