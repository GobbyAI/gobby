from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.tasks import expansion_qa_coverage
from tests.workflows.expansion_qa_helpers import call_args, covered_report, make_expansion_qa_case

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_PATH = PROJECT_ROOT / "src/gobby/install/shared/workflows/agents/expansion-qa.yaml"


@pytest.mark.asyncio
async def test_workflow_calls_evaluate_with_full_scope(
    temp_db,
    project_manager,
    temp_dir,
    monkeypatch,
) -> None:
    case = make_expansion_qa_case(temp_db, project_manager, temp_dir)
    captured: dict[str, object] = {}

    def fake_evaluate(**kwargs):
        captured.update(kwargs)
        return covered_report()

    monkeypatch.setattr(expansion_qa_coverage, "_evaluate_with_a4", fake_evaluate)
    monkeypatch.setattr(expansion_qa_coverage, "_load_a4_manifest_writer", lambda: None)

    result = await case["registry"].call("run_expansion_qa_coverage", call_args(case))

    assert result["ok"] is True
    assert captured["plan_path"] == case["plan_path"]
    assert captured["plan_id"] == "task-qa-plan"
    assert captured["plan_hash"] == case["plan_hash"]
    assert captured["root_task_ref"] == case["root_task"]
    assert captured["project_id"] == case["project"].id
    assert captured["task_tree"] == "db"


def test_expansion_qa_yaml_wires_coverage_gate() -> None:
    agent = yaml.safe_load(AGENT_PATH.read_text(encoding="utf-8"))
    step = next(step for step in agent["steps"] if step["name"] == "coverage_check")
    instructions = agent["instructions"]

    assert "run_expansion_qa_coverage" in instructions
    assert "--plan <plan_path>" in instructions
    assert "--plan-id <plan_id>" in instructions
    assert "--plan-hash <plan_hash>" in instructions
    assert "--root-task <root_task>" in instructions
    assert "--project-id <project_id>" in instructions
    assert "--task-tree db" in instructions
    assert any(hook["tool"] == "run_expansion_qa_coverage" for hook in step["on_mcp_success"])


def test_manifest_path_components_are_capped() -> None:
    sanitized = expansion_qa_coverage._sanitize("x" * 90, kind="plan_id")

    assert len(sanitized.encode("utf-8")) <= 64
    assert sanitized.startswith("x")
    assert "-" in sanitized
