"""Pipeline/scheduling references preserve their operating contracts."""

from pathlib import Path

import pytest
import yaml

from gobby.skills.capability_catalog import load_capability_catalog
from gobby.skills.loader import SkillLoader
from gobby.workflows.definitions import PipelineDefinition

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills"
REFERENCES = ROOT / "gobby/references/pipelines"


def _body() -> str:
    return "\n".join(path.read_text() for path in sorted(REFERENCES.glob("*.md")))


def test_metadata_is_discoverable_and_authoring_category() -> None:
    catalog = load_capability_catalog()
    capability = next(item for item in catalog.capabilities if item.name == "pipelines")
    assert capability.description and capability.when
    assert {topic.name for topic in capability.topics} >= {"authoring", "scheduling", "execution"}
    assert catalog.folded_skills["pipelines-and-cron"] == "gobby:references/pipelines/overview.md"


def test_bundled_directory_discovery_finds_pipelines_and_cron() -> None:
    names = {skill.name for skill in SkillLoader().load_directory(ROOT)}
    assert "gobby" in names
    assert "pipelines-and-cron" not in names
    assert "gobby-workflows:list_pipelines" in _body()


def test_pipeline_yaml_example_matches_runtime_definition() -> None:
    block = (REFERENCES / "validation.md").read_text().split("```yaml", 1)[1].split("```", 1)[0]
    pipeline = PipelineDefinition.model_validate(yaml.safe_load(block))
    assert pipeline.name == "reference-check"
    assert [step.id for step in pipeline.steps] == ["inspect", "publish"]
    assert pipeline.steps[1].approval is not None
    assert pipeline.steps[1].approval.required is True


def test_documents_current_pipeline_tool_lifecycle() -> None:
    for name in ("create_pipeline", "run_pipeline", "get_pipeline_status", "update_pipeline"):
        assert name in _body()


def test_documents_complete_cron_tool_family() -> None:
    for name in (
        "list_cron_jobs",
        "create_cron_job",
        "get_cron_job",
        "update_cron_job",
        "toggle_cron_job",
        "delete_cron_job",
        "run_cron_job",
        "list_cron_runs",
    ):
        assert name in _body()


def test_separates_automation_paths_and_omits_retired_content() -> None:
    body = " ".join(_body().split())
    assert "pipelines for ordered steps, cron for timing" in body
    assert "build capability for task lifecycle dispatch" in body
    assert "without moving task ownership" in body
    assert "## Agent Definitions" not in body
    assert "mode: terminal" not in body
    assert "mark_task_" not in body
    assert "save_expansion_spec" not in body
