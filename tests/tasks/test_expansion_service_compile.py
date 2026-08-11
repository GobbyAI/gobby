"""Parser-driven task expansion compile tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.feature_base import candidate_labels
from gobby.config.tasks import TaskExpansionConfig
from gobby.plans.parser import Kind, PlanDocument, parse_plan
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.expansion._compile import _build_file_context, _expansion_feature_config
from gobby.tasks.expansion_service import ExpansionService
from gobby.tasks.generation_schemas import EXPANSION_COMPILATION_SCHEMA

pytestmark = pytest.mark.unit


@pytest.fixture
def service(temp_db) -> ExpansionService:
    return ExpansionService(task_manager=LocalTaskManager(temp_db), llm_service=MagicMock())


def _parent(service: ExpansionService, sample_project: dict[str, Any]) -> Task:
    return service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Lifecycle dispatch",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )


def _regression_plan_path() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures/plans/expansion-compile-regression.md"


def _regression_plan_doc() -> PlanDocument:
    return parse_plan(_regression_plan_path(), parse_mode="expansion")


def _deps_for(spec: dict[str, Any], task_id: str) -> set[str]:
    return {edge["depends_on"] for edge in spec["dependencies"] if edge["task_id"] == task_id}


def test_build_file_context_rejects_parent_traversal(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / "src").mkdir(parents=True)
    (repo_path / "src" / "safe.txt").write_text("safe content", encoding="utf-8")
    (repo_path / "decoy.txt").write_text("decoy content", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret content", encoding="utf-8")
    task = SimpleNamespace(
        title="Read src/safe.txt",
        description=(
            "Also read ../../decoy.txt, src/../../secret.txt, and "
            "src/../../../../../../../etc/ssh/sshd_config"
        ),
        validation_criteria="",
    )

    context = _build_file_context(None, task, repo_path)

    assert "safe content" in context
    assert "decoy content" not in context
    assert "secret content" not in context
    assert "sshd_config" not in context


def test_build_file_context_rejects_symlink_outside_repo(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / "src").mkdir(parents=True)
    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("secret content", encoding="utf-8")
    (repo_path / "src" / "linked.txt").symlink_to(secret_path)
    task = SimpleNamespace(
        title="Read src/linked.txt",
        description="",
        validation_criteria="",
    )

    context = _build_file_context(None, task, repo_path)

    assert context == ""


def test_build_file_context_includes_related_existing_tests(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    test_path = repo_path / "tests" / "tasks" / "test_widget_compiler.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_widget_compiles():\n    assert True\n", encoding="utf-8")
    task = SimpleNamespace(
        title="Compile widgets",
        description="Improve widget compilation behavior",
        validation_criteria="Widget compiler tests pass",
    )

    context = _build_file_context(None, task, repo_path)

    assert "## Related existing test files" in context
    assert "### tests/tasks/test_widget_compiler.py" in context
    assert "def test_widget_compiles()" in context


def test_build_file_context_omits_empty_related_test_section(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    unrelated_path = repo_path / "tests" / "test_unrelated.py"
    unrelated_path.parent.mkdir(parents=True)
    unrelated_path.write_text("def test_unrelated():\n    assert True\n", encoding="utf-8")
    task = SimpleNamespace(
        title="Compile widgets",
        description="Improve widget compilation behavior",
        validation_criteria="Widget compiler tests pass",
    )

    context = _build_file_context(None, task, repo_path)

    assert context == ""


def test_build_file_context_limits_related_tests_to_eight(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    tests_path = repo_path / "tests" / "tasks"
    tests_path.mkdir(parents=True)
    for index in range(10):
        (tests_path / f"test_widget_compiler_{index}.py").write_text(
            f"def test_widget_compiler_{index}():\n    assert True\n",
            encoding="utf-8",
        )
    task = SimpleNamespace(
        title="Compile widgets",
        description="Improve widget compilation behavior",
        validation_criteria="Widget compiler tests pass",
    )

    context = _build_file_context(None, task, repo_path)

    assert context.count("### tests/tasks/test_widget_compiler_") == 8
    assert "### tests/tasks/test_widget_compiler_7.py" in context
    assert "### tests/tasks/test_widget_compiler_8.py" not in context


def test_expansion_feature_config_resolves_structured_candidate_overrides() -> None:
    expansion_config = TaskExpansionConfig(
        candidates=[
            {"candidate": "claude/sonnet", "reasoning_effort": "high"},
            {"candidate": "codex/gpt-5.4", "reasoning_effort": "xhigh"},
        ]
    )

    provider_only = _expansion_feature_config(
        expansion_config,
        SimpleNamespace(provider="codex", model=None),
    )
    model_only = _expansion_feature_config(
        expansion_config,
        SimpleNamespace(provider=None, model="sonnet"),
    )

    assert candidate_labels(provider_only.candidates) == ("codex/gpt-5.4",)
    assert candidate_labels(model_only.candidates) == ("claude/sonnet",)


def test_compile_contract_plan_emits_tdd_leaves_by_phase(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_doc = _regression_plan_doc()
    deliverable_count = sum(1 for section in plan_doc.sections if section.kind is Kind.deliverable)

    spec = service.compile_plan_to_spec(plan_doc, parent)

    assert len(plan_doc.manifest_entries) == deliverable_count
    assert spec["contract_plan"] is True
    assert spec["deliverable_count"] == 6
    assert len(spec["tasks"]) == deliverable_count
    assert {phase["id"]: len(phase["task_ids"]) for phase in spec["phases"]} == {
        "phase-p1": 3,
        "phase-p2": 2,
        "phase-p3": 1,
    }
    assert all(not any(key.startswith("tdd_") for key in phase) for phase in spec["phases"])
    assert spec["tdd_mode"] == "skill_backed"
    assert not any(
        task["title"].startswith(("[TEST]", "[REF]", "[IMPL]")) for task in spec["tasks"]
    )


def test_compile_contract_plan_emits_covers_labels_for_each_tdd_leaf(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_doc = _regression_plan_doc()
    plan_id = _regression_plan_path().stem
    expected_labels = {
        section.section_id: {
            f"covers:{plan_id}:{section.section_id}:{item.item_id}"
            for item in section.acceptance_items
        }
        for section in plan_doc.sections
        if section.kind is Kind.deliverable
    }

    spec = service.compile_plan_to_spec(plan_doc, parent)

    # Per-entry tasks carry their section's covers labels plus TDD metadata.
    for task in spec["tasks"]:
        section_id = task["source_section_id"]
        expected = set(expected_labels[section_id])
        if task["tdd_required"]:
            expected.add("tdd:required")
        assert set(task["labels"]) == expected
        assert expected_labels[section_id]


def test_compile_contract_plan_translates_section_dependencies(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(_regression_plan_doc(), parent)

    # Cross-deliverable depends_on links single-task leaves directly.
    assert _deps_for(spec, "1.1::single") == set()
    assert _deps_for(spec, "1.2::single") == {"1.1::single"}
    assert "1.2::single" in _deps_for(spec, "1.3a::single")
    # 2.1 depends_on 1.3a (single).
    assert "1.3a::single" in _deps_for(spec, "2.1::single")
    # 2.2 depends_on 1.2.
    assert "1.2::single" in _deps_for(spec, "2.2::single")
    # 3.1 depends_on both 2.1 and 2.2.
    assert {"2.1::single", "2.2::single"} <= _deps_for(spec, "3.1::single")


def test_compile_contract_plan_uses_manifest_agent_assignment(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    spec = service.compile_plan_to_spec(_regression_plan_doc(), parent)

    # Per-entry tasks carry the manifest's assigned_agent verbatim.
    section_tasks = [task for task in spec["tasks"] if task["source_section_id"] is not None]
    frontend_sections = {
        task["source_section_id"]
        for task in section_tasks
        if task["assigned_agent"] == "frontend-developer"
    }
    assert frontend_sections == {"2.1"}
    assert {
        task["assigned_agent"]
        for task in section_tasks
        if task["source_section_id"] not in frontend_sections
    } == {"backend-developer"}
    assert {
        task["source_section_id"]
        for task in section_tasks
        if task["additional_skills"] == ["test-driven-development"]
    } == {"1.1", "1.2", "2.1", "2.2"}
    assert all("[category:" not in task["title"] for task in spec["tasks"])
    assert all("(depends:" not in task["title"] for task in spec["tasks"])


def test_compile_assigns_agent_per_manifest_entry(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_doc = _regression_plan_doc()
    spec = service.compile_plan_to_spec(plan_doc, parent)

    expected_agents = {
        entry.source_section: entry.assigned_agent for entry in plan_doc.manifest_entries
    }

    assigned_by_section = {
        task["source_section_id"]: task["assigned_agent"]
        for task in spec["tasks"]
        if task["source_section_id"] is not None
    }

    assert assigned_by_section == expected_agents
    assert assigned_by_section["2.1"] == "frontend-developer"
    assert assigned_by_section["1.1"] == "backend-developer"
    assert all(task["assigned_agent"] for task in spec["tasks"])


def test_compile_contract_plan_prefers_manifest_assigned_agent_over_prose_regex(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_path = Path(__file__).resolve().parents[1] / "fixtures/plans/manifest-routing-bridge.md"
    spec = service.compile_plan_to_spec(parse_plan(plan_path, parse_mode="draft"), parent)

    # Section 2.1 emits one implementation leaf.
    section_tasks = [task for task in spec["tasks"] if task["source_section_id"] == "2.1"]
    assert len(section_tasks) == 1
    assert {task["assigned_agent"] for task in section_tasks} == {"backend-developer"}
    assert all(task["additional_skills"] == ["test-driven-development"] for task in section_tasks)


def test_compile_12898_contract_plan_preserves_manifest_deliverables(
    service: ExpansionService,
    sample_project,
) -> None:
    parent = _parent(service, sample_project)
    plan_path = (
        Path(__file__).resolve().parents[2]
        / ".gobby/plans/completed/task-12898-memory-recall-helper.md"
    )
    spec = service.compile_plan_to_spec(parse_plan(plan_path, parse_mode="expansion"), parent)

    assert spec["deliverable_count"] == 14
    assert len(spec["tasks"]) == 14
    assert len(spec["phases"]) == 3
    assert any(
        task["source_section_id"] == "2.6" and "notify_parent_on_completion" in task["title"]
        for task in spec["tasks"]
    )


def test_compile_rejects_missing_manifest_entry(
    service: ExpansionService,
    sample_project,
    tmp_path: Path,
) -> None:
    parent = _parent(service, sample_project)
    plan = tmp_path / "missing-entry.md"
    plan.write_text(
        """
> **Plan ID:** missing-entry

## 1.1 Implement thing
`kind: deliverable`

**Acceptance:**
- 1.1.1 - Thing exists. file: `src/thing.py`

""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest entries") as excinfo:
        service.compile_plan_to_spec(parse_plan(plan, parse_mode="draft"), parent)

    assert "1.1" in str(excinfo.value)


@pytest.mark.asyncio
async def test_invoke_llm_compile_forwards_schema(service: ExpansionService) -> None:
    service.llm_service.call_json_feature = AsyncMock(return_value={})
    service._render_prompt = MagicMock(return_value="prompt")
    run = MagicMock(id="expand-1", provider=None, model=None)

    result = await service._invoke_llm_compile(run, {"task": {}})

    assert result == {}
    assert (
        service.llm_service.call_json_feature.await_args.kwargs["json_schema"]
        == EXPANSION_COMPILATION_SCHEMA
    )


@pytest.mark.asyncio
async def test_invoke_llm_compile_wraps_json_decode_error(service: ExpansionService) -> None:
    service.llm_service.call_json_feature.side_effect = json.JSONDecodeError("bad", "x", 0)
    service._render_prompt = MagicMock(return_value="prompt")
    run = MagicMock(id="expand-1", provider=None, model=None)

    with pytest.raises(ValueError, match="did not return valid JSON"):
        await service._invoke_llm_compile(run, {"task": {}})


@pytest.mark.asyncio
async def test_invoke_llm_compile_wraps_value_error(service: ExpansionService) -> None:
    service.llm_service.call_json_feature.side_effect = ValueError("bad json")
    service._render_prompt = MagicMock(return_value="prompt")
    run = MagicMock(id="expand-1", provider=None, model=None)

    with pytest.raises(ValueError, match="did not return valid JSON") as excinfo:
        await service._invoke_llm_compile(run, {"task": {}})

    assert isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_invoke_llm_compile_wraps_non_object_result(service: ExpansionService) -> None:
    service.llm_service.call_json_feature = AsyncMock(return_value=[])
    service._render_prompt = MagicMock(return_value="prompt")
    run = MagicMock(id="expand-1", provider=None, model=None)

    with pytest.raises(ValueError, match="did not return valid JSON") as excinfo:
        await service._invoke_llm_compile(run, {"task": {}})

    assert "expected JSON object" in str(excinfo.value.__cause__)


@pytest.mark.asyncio
async def test_invoke_llm_compile_wraps_unexpected_provider_error(
    service: ExpansionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.llm_service.call_json_feature.side_effect = RuntimeError("provider down")
    monkeypatch.setattr(service, "_render_prompt", MagicMock(return_value="prompt"))
    run = MagicMock(id="expand-1", provider=None, model=None)

    with pytest.raises(ValueError, match="Expansion compiler failed for run=expand-1") as excinfo:
        await service._invoke_llm_compile(run, {"task": {}})

    assert isinstance(excinfo.value.__cause__, RuntimeError)
