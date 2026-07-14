"""Contract tests for the bundled pipelines-and-cron authoring skill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from gobby.install.manifest import hash_file_bytes
from gobby.skills.loader import SkillLoader
from gobby.workflows.definitions import PipelineDefinition

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/pipelines-and-cron"
SKILLS_ROOT = REPO_ROOT / "src/gobby/install/shared/skills"


def _body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter() -> dict:
    header = _body().split("---", 2)[1]
    data = yaml.safe_load(header)
    assert isinstance(data, dict)
    return data


def _pipeline_example() -> dict:
    block = _body().split("```yaml", 1)[1].split("```", 1)[0]
    data = yaml.safe_load(block)
    assert isinstance(data, dict)
    return data


def test_metadata_is_discoverable_and_authoring_category() -> None:
    frontmatter = _frontmatter()
    skill = SkillLoader().load_skill(SKILL_DIR)

    assert frontmatter["name"] == "pipelines-and-cron"
    assert frontmatter["description"].startswith("Use when")
    assert frontmatter["category"] == "authoring"
    assert frontmatter["metadata"]["gobby"]["audience"] == "all"
    assert skill.name == "pipelines-and-cron"
    assert skill.get_category() == "authoring"


def test_bundled_directory_discovery_finds_pipelines_and_cron() -> None:
    skills = SkillLoader().load_directory(SKILLS_ROOT)

    assert "pipelines-and-cron" in {skill.name for skill in skills}


def test_bundled_manifest_tracks_pipelines_and_cron() -> None:
    manifest_path = REPO_ROOT / "src/gobby/install/bundled_content_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["files"]["skills/pipelines-and-cron/SKILL.md"] == hash_file_bytes(
        SKILL_DIR / "SKILL.md"
    )


def test_pipeline_yaml_example_matches_runtime_definition() -> None:
    pipeline = PipelineDefinition.model_validate(_pipeline_example())

    assert pipeline.name == "release-check"
    assert [step.id for step in pipeline.steps] == ["test", "deploy"]
    assert pipeline.steps[1].approval is not None
    assert pipeline.steps[1].approval.required is True


def test_documents_current_pipeline_tool_lifecycle() -> None:
    body = _body()

    for tool_name in (
        "create_pipeline",
        "run_pipeline",
        "get_pipeline_status",
        "update_pipeline",
    ):
        assert tool_name in body


def test_documents_complete_cron_tool_family() -> None:
    body = _body()

    for tool_name in (
        "list_cron_jobs",
        "create_cron_job",
        "get_cron_job",
        "update_cron_job",
        "toggle_cron_job",
        "delete_cron_job",
        "run_cron_job",
        "list_cron_runs",
    ):
        assert tool_name in body


def test_separates_automation_paths_and_omits_retired_content() -> None:
    body = _body()
    normalized = body.lower()

    assert "deterministic multi-step" in normalized
    assert "scheduled" in normalized
    assert "task-lifecycle automation" in normalized
    assert "gobby build" in body
    assert "dispatch" in body
    assert "## Agent Definitions" not in body
    assert "mode: terminal" not in body
    assert "mark_task_" not in body
    assert "save_expansion_spec" not in body
