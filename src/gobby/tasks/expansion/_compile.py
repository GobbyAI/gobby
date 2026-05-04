"""Compile path for task expansion runs."""

# mypy: disable-error-code="no-any-return"

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from gobby.plans.parser import Kind
from gobby.storage.expansion_runs import ExpansionRun
from gobby.storage.tasks import Task
from gobby.tasks.commits import extract_mentioned_files
from gobby.tasks.expansion._common import (
    _agent_selection_fields,
    _dedupe_dependencies,
    _dev_is_only_enabled_stage,
    _manifest_stage_names,
    _read_text_if_exists,
    _render_template,
    _strip_frontmatter,
    list_agent_definitions,
)
from gobby.utils.json_helpers import extract_json_object
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)
_BUNDLED_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "install" / "shared" / "prompts"


async def compile_run(self: Any, run_id: str) -> ExpansionRun:
    """Compile an expansion run into a normalized compiled spec."""
    run = self.run_manager.get(run_id)
    if run is None:
        raise ValueError(f"Expansion run {run_id} not found")
    task = self.task_manager.get_task(run.parent_task_id)
    if task is None:
        raise ValueError(f"Parent task {run.parent_task_id} not found")
    self.run_manager.start(run_id)
    self.run_manager.append_log(run_id, level="info", message="Starting expansion compile")
    if _dev_is_only_enabled_stage(task):
        return self._complete_dev_only_run(run_id, task)

    plan_doc = self._parse_contract_plan(run, task)
    if plan_doc is not None:
        deliverable_count = sum(
            1 for section in plan_doc.sections if section.kind is Kind.deliverable
        )
        if deliverable_count == 0:
            raise ValueError(
                f"Contract plan file has no kind: deliverable sections: {plan_doc.source_path}"
            )
        self.run_manager.append_log(
            run_id,
            level="info",
            message="Detected Plan-Coverage Contract plan; compiling deterministically",
            extra={"plan_file": str(plan_doc.source_path)},
        )
        compiled_spec = self.compile_plan_to_spec(plan_doc, task)
    else:
        raw_spec = await self._generate_raw_spec(run, task)
        compiled_spec = self.normalize_compiled_spec(raw_spec, task=task, plan_file=None)
    validation = self.validate_compiled_spec(compiled_spec)
    if not validation["valid"]:
        errors = "; ".join(validation["errors"])
        raise ValueError(f"Compiled expansion spec failed validation: {errors}")

    self.run_manager.save_compiled_spec(
        run_id,
        compiled_spec,
        checkpoints={"compile_validation": validation},
    )
    self.run_manager.append_log(
        run_id,
        level="info",
        message="Expansion compile completed",
        extra={
            "task_count": len(compiled_spec["tasks"]),
            "phase_count": len(compiled_spec["phases"]),
        },
    )
    refreshed = self.run_manager.get(run_id)
    if refreshed is None:
        raise RuntimeError(f"Expansion run {run_id} disappeared after compile")
    return refreshed


async def compile_and_apply_run(
    self: Any,
    run_id: str,
    *,
    session_id: str | None,
    auto_apply: bool = True,
) -> ExpansionRun:
    """Compile a run and optionally apply it."""
    run = self.run_manager.get(run_id)
    if run is None:
        raise ValueError(f"Expansion run {run_id} not found")
    if run.compiled_spec is None:
        run = await self.compile_run(run_id)
    if run.compiled_spec is None and run.status == "completed":
        return run
    if auto_apply:
        return self.apply_run(run.id, session_id=session_id)
    return run


def normalize_compiled_spec(
    self: Any,
    raw_spec: dict[str, Any],
    *,
    task: Task,
    plan_file: str | None,
) -> dict[str, Any]:
    """Normalize ad-hoc LLM output into the compiled expansion schema."""
    if "phases" not in raw_spec or "tasks" not in raw_spec:
        raise ValueError("Expansion compiler must return {phases,tasks}")
    return self._normalize_native_compiled_spec(raw_spec, task=task, plan_file=plan_file)


def _list_agent_definitions_for_selection(
    self: Any,
    project_id: str | None,
) -> list[dict[str, Any]]:
    """List spawn-capable agent definitions once per project for expansion selection."""
    if project_id not in self._agent_definition_cache:
        result = list_agent_definitions(
            self.definition_manager,
            enabled=True,
            project_id=project_id,
            surface_filter="spawn",
        )
        raw_agents = result.get("agents") if result.get("success") else []
        agents = raw_agents if isinstance(raw_agents, list) else []
        self._agent_definition_cache[project_id] = [
            agent for agent in agents if isinstance(agent, dict)
        ]
    return self._agent_definition_cache[project_id]


async def _generate_raw_spec(self: Any, run: ExpansionRun, task: Task) -> dict[str, Any]:
    """Call the configured LLM and return raw JSON output."""
    prompt_context = self._build_prompt_context(run, task)
    return await self._invoke_llm_compile(run, prompt_context)


async def _invoke_llm_compile(
    self: Any,
    run: ExpansionRun,
    prompt_context: dict[str, Any],
    *,
    phase_number: int | None = None,
) -> dict[str, Any]:
    """Render prompts, call the configured LLM, and parse the JSON response."""
    if self.llm_service is None:
        raise RuntimeError("LLM service is unavailable for task expansion")

    expansion_config = self._get_expansion_config()
    prompt_path = (
        expansion_config.prompt_path
        if expansion_config and expansion_config.prompt_path
        else "expansion/user"
    )
    system_path = (
        expansion_config.system_prompt_path
        if expansion_config and expansion_config.system_prompt_path
        else "expansion/system"
    )
    system_prompt = self._render_prompt(system_path, {"tdd_mode": True, **prompt_context})
    user_prompt = self._render_prompt(prompt_path, prompt_context)
    provider_name = run.provider or (expansion_config.provider if expansion_config else "claude")
    model_name = run.model or (expansion_config.model if expansion_config else "opus")

    provider = self.llm_service.get_provider(provider_name)
    scope = f"run={run.id}" + (f" phase={phase_number}" if phase_number is not None else "")
    try:
        result = await provider.generate_json(
            user_prompt, system_prompt=system_prompt, model=model_name
        )
        return cast(dict[str, Any], result)
    except Exception as e:
        logger.debug(
            "generate_json failed for expansion %s; falling back to generate_text",
            scope,
            exc_info=True,
        )
        response_text = await provider.generate_text(
            user_prompt,
            system_prompt=system_prompt,
            model=model_name,
            max_tokens=8000,
            caller="tasks.expansion.text_fallback",
        )
        parsed = extract_json_object(response_text)
        if parsed is None:
            raise ValueError("Expansion compiler did not return valid JSON") from e
        return parsed


def _build_prompt_context(self: Any, run: ExpansionRun, task: Task) -> dict[str, Any]:
    """Build prompt context for ad-hoc expansion compilation."""
    repo_path = self._resolve_repo_path(task)
    verification = self._get_verification_commands(repo_path)
    file_context = self._build_file_context(task, repo_path)

    verification_lines = []
    for name, command in verification.items():
        verification_lines.append(f"- `{name}`: `{command}`")
    verification_str = (
        "\n".join(verification_lines)
        if verification_lines
        else "- No verification commands configured."
    )

    research_sections: list[str] = [f"Project verification commands:\n{verification_str}"]
    if file_context:
        research_sections.append(file_context)

    enabled_stages = sorted(_manifest_stage_names(task)) if task.stages else []
    return {
        "task_id": task.id,
        "title": task.title,
        "description": task.description or "",
        "enabled_stages": enabled_stages,
        "context_str": "\n\n".join(research_sections),
        "research_str": file_context or "No repository files were selected for context.",
    }


def _normalize_native_compiled_spec(
    self: Any,
    raw_spec: dict[str, Any],
    *,
    task: Task,
    plan_file: str | None,
) -> dict[str, Any]:
    """Normalize already-compiled LLM output into the canonical schema."""
    raw_tasks = list(raw_spec.get("tasks") or [])
    if not raw_tasks:
        raise ValueError("Compiled spec returned no tasks")

    tasks: list[dict[str, Any]] = []
    phase_list = list(raw_spec.get("phases") or [])
    if not phase_list:
        phase_list = [{"id": "phase-1", "title": task.title, "summary": task.description or ""}]

    phase_ids = [phase.get("id") or f"phase-{i + 1}" for i, phase in enumerate(phase_list)]
    phase_order = {phase_id: i + 1 for i, phase_id in enumerate(phase_ids)}
    normalized_phases: list[dict[str, Any]] = []
    for i, phase in enumerate(phase_list):
        phase_id = phase.get("id") or f"phase-{i + 1}"
        normalized_phases.append(
            {
                "id": phase_id,
                "title": phase.get("title") or f"Phase {phase_order[phase_id]}",
                "summary": phase.get("summary") or "",
                "test_intent": {
                    "summary": (phase.get("test_intent") or {}).get("summary")
                    or f"Verify Phase {phase_order[phase_id]} behavior",
                    "behaviors": list((phase.get("test_intent") or {}).get("behaviors") or []),
                    "suggested_test_files": list(
                        (phase.get("test_intent") or {}).get("suggested_test_files") or []
                    ),
                    "entry_criteria": list(
                        (phase.get("test_intent") or {}).get("entry_criteria") or []
                    ),
                },
                "task_ids": [],
            }
        )

    phase_by_id = {phase["id"]: phase for phase in normalized_phases}
    dependencies: list[dict[str, str]] = []
    agent_definitions = self._list_agent_definitions_for_selection(task.project_id)
    for i, task_item in enumerate(raw_tasks):
        phase_id = task_item.get("phase_id") or normalized_phases[0]["id"]
        stable_id = task_item.get("id") or f"task-{i + 1:03d}"
        affected_files = list(task_item.get("affected_files") or [])
        assigned_agent, additional_skills, description = _agent_selection_fields(
            task_item,
            agent_definitions,
        )
        normalized_task = {
            "id": stable_id,
            "phase_id": phase_id,
            "title": task_item.get("title") or f"Task {i + 1}",
            "description": description,
            "priority": int(task_item.get("priority", 2)),
            "task_type": task_item.get("task_type", "task"),
            "category": task_item.get("category", "code"),
            "validation": task_item.get("validation") or task_item.get("validation_criteria"),
            "affected_files": affected_files,
            "execution_group": task_item.get("execution_group") or task_item.get("parallel_group"),
            "assigned_agent": assigned_agent,
            "additional_skills": additional_skills,
        }
        tasks.append(normalized_task)
        phase_by_id.setdefault(
            phase_id,
            {
                "id": phase_id,
                "title": f"Phase {len(phase_by_id) + 1}",
                "summary": "",
                "test_intent": {
                    "summary": f"Verify {normalized_task['title']}",
                    "behaviors": [],
                    "suggested_test_files": [],
                    "entry_criteria": [],
                },
                "task_ids": [],
            },
        )
        phase_by_id[phase_id]["task_ids"].append(stable_id)

        for blocker in task_item.get("depends_on") or []:
            if isinstance(blocker, str):
                dependencies.append({"task_id": stable_id, "depends_on": blocker})

    for edge in raw_spec.get("dependencies") or []:
        task_id = edge.get("task_id")
        depends_on = edge.get("depends_on")
        if task_id and depends_on:
            dependencies.append({"task_id": task_id, "depends_on": depends_on})

    execution_groups = []
    group_index: dict[str, list[str]] = defaultdict(list)
    for task_item in tasks:
        if task_item.get("execution_group"):
            group_index[task_item["execution_group"]].append(task_item["id"])
    for group_name, task_ids in group_index.items():
        execution_groups.append({"id": group_name, "mode": "parallel", "task_ids": task_ids})

    return {
        "version": 1,
        "parent_task_id": task.id,
        "plan_file": plan_file or raw_spec.get("plan_file"),
        "phases": list(phase_by_id.values()),
        "tasks": tasks,
        "dependencies": _dedupe_dependencies(dependencies),
        "execution_groups": execution_groups,
    }


def _render_prompt(self: Any, path: str, context: dict[str, Any]) -> str:
    """Render a prompt from DB-backed prompts with a bundled-file fallback."""
    try:
        return self.prompt_loader.render(path, context)
    except FileNotFoundError:
        prompt_file = _BUNDLED_PROMPTS_DIR / f"{path}.md"
        raw_content = _read_text_if_exists(prompt_file)
        if raw_content is None:
            raise
        return _render_template(_strip_frontmatter(raw_content), context)


def _resolve_repo_path(self: Any, task: Task) -> Path | None:
    """Resolve the repository path for the task's project."""
    project = self.project_manager.get(task.project_id)
    if project and project.repo_path:
        return Path(project.repo_path)
    project_ctx = get_project_context()
    if project_ctx and project_ctx.get("project_path"):
        return Path(project_ctx["project_path"])
    return None


def _get_verification_commands(self: Any, repo_path: Path | None) -> dict[str, str]:
    """Resolve project verification commands from project.json or daemon defaults."""
    project_ctx = (
        get_project_context(cwd=repo_path) if repo_path is not None else get_project_context()
    )
    verification = project_ctx.get("verification") if project_ctx else None
    if isinstance(verification, dict):
        return {key: str(value) for key, value in verification.items() if value}
    if self.config is not None:
        return self.config.get_verification_defaults().all_commands()
    return {}


def _build_file_context(self: Any, task: Task, repo_path: Path | None) -> str:
    """Build a focused repository context block for expansion prompts."""
    if repo_path is None:
        return ""
    task_payload = {
        "title": task.title,
        "description": task.description or "",
        "validation_criteria": task.validation_criteria,
    }
    mentioned_files = extract_mentioned_files(task_payload)

    unique_files: list[str] = []
    seen: set[str] = set()
    for file_path in mentioned_files:
        normalized = file_path.lstrip("./")
        if normalized in seen:
            continue
        absolute = repo_path / normalized
        if absolute.exists() and absolute.is_file():
            unique_files.append(normalized)
            seen.add(normalized)
        if len(unique_files) >= 8:
            break

    if not unique_files:
        return ""

    sections: list[str] = []
    for file_path in unique_files:
        content = _read_text_if_exists(repo_path / file_path, max_chars=3500)
        if content:
            sections.append(f"### {file_path}\n{content}")
    return "\n\n".join(sections)
