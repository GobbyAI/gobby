"""Compile path for task expansion runs."""

# mypy: disable-error-code="no-any-return"

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from gobby.plans.parser import (
    Kind,
    ManifestEntry,
    PlanDocument,
    PlanParseError,
    PlanSection,
    parse_plan,
)
from gobby.storage.expansion_runs import ExpansionRun
from gobby.storage.tasks import Task
from gobby.tasks.commits import extract_mentioned_files
from gobby.tasks.expansion._common import (
    _CONTRACT_PHASE_ID_RE,
    _DEFAULT_PHASE_ID,
    AUTOMATED_LEAF_CATEGORIES,
    _agent_selection_fields,
    _clean_contract_section_title,
    _contract_acceptance_lines,
    _contract_affected_files,
    _contract_deferral_record,
    _contract_phase_number,
    _contract_phase_spec_id,
    _contract_plan_id,
    _contract_section_body,
    _contract_single_task_id,
    _contract_task_ids,
    _dev_is_only_enabled_stage,
    _find_test_files,
    _read_text_if_exists,
    _render_template,
    _skipped_stages,
    _stable_ref_id,
    _stable_test_id,
    _strip_frontmatter,
    list_agent_definitions,
)
from gobby.utils.json_helpers import extract_json_object
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)
_BUNDLED_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "install" / "shared" / "prompts"


def validate_plan_file(self: Any, plan_path: Path) -> dict[str, Any]:
    """Validate a plan file against the Plan-Coverage Contract."""
    if not plan_path.exists():
        return {"valid": False, "errors": [f"Plan file not found: {plan_path}"]}
    try:
        plan_doc = parse_plan(plan_path, parse_mode="draft")
    except (OSError, PlanParseError) as exc:
        return {"valid": False, "errors": [f"Plan file is not contract-conforming: {exc}"]}
    deliverables = [section for section in plan_doc.sections if section.kind is Kind.deliverable]
    if not deliverables:
        return {
            "valid": False,
            "errors": [f"Plan file has no kind: deliverable sections: {plan_path}"],
        }
    phases = {
        _contract_phase_number(section.section_id): _clean_contract_section_title(section.title)
        for section in plan_doc.sections
        if _CONTRACT_PHASE_ID_RE.match(section.section_id)
    }
    return {
        "valid": True,
        "path": str(plan_path),
        "phase_count": len(phases),
        "phases": phases,
        "deliverable_count": len(deliverables),
        "contract_plan": True,
    }


def compile_plan_to_spec(self: Any, plan_doc: PlanDocument, task: Task) -> dict[str, Any]:
    """Compile a Plan-Coverage Contract document into a compiled expansion spec.

    TDD shape (per ``docs/guides/tdd-enforcement.md``): each phase containing
    TDD-eligible (``tdd: true``) manifest entries is wrapped with a single
    phase-level ``[TEST] Phase N: Write failing tests`` task at the start
    and a single ``[REF] Phase N: Refactor with green tests`` task at the
    end. Per-entry ``[IMPL]`` tasks sit in the middle. Non-TDD entries
    emit single tasks without a prefix. Cross-phase: phase ``N+1``'s
    ``[TEST]`` depends on phase ``N``'s ``[REF]``. Cross-deliverable
    ``depends_on`` edges link IMPL/single tasks directly.
    """
    plan_id = _contract_plan_id(plan_doc)
    section_by_id = {section.section_id: section for section in plan_doc.sections}
    phase_by_section_id = self._contract_phase_index(plan_doc)
    manifest_entry_by_section = self._validate_contract_manifest(plan_doc, section_by_id)

    phases: list[dict[str, Any]] = []
    phase_by_id: dict[str, dict[str, Any]] = {}
    tasks: list[dict[str, Any]] = []
    dependencies: list[dict[str, str]] = []
    deferrals = self._contract_deferrals(plan_doc)

    # Group manifest entries by phase id, preserving declaration order.
    entries_by_phase_id: dict[str, list[ManifestEntry]] = {}
    phase_section_by_phase_id: dict[str, PlanSection | None] = {}
    phase_id_order: list[str] = []
    for entry in plan_doc.manifest_entries:
        phase_section = phase_by_section_id.get(entry.source_section)
        phase_source_id = (
            phase_section.section_id if phase_section is not None else _DEFAULT_PHASE_ID
        )
        phase_id = _contract_phase_spec_id(phase_source_id)
        if phase_id not in entries_by_phase_id:
            entries_by_phase_id[phase_id] = []
            phase_section_by_phase_id[phase_id] = phase_section
            phase_id_order.append(phase_id)
        entries_by_phase_id[phase_id].append(entry)

    # entry.source_section -> id of the per-entry IMPL or single task.
    # Used to wire cross-deliverable depends_on edges below.
    entry_work_task_id: dict[str, str] = {}
    prior_phase_ref_id: str | None = None

    for phase_id in phase_id_order:
        phase_section = phase_section_by_phase_id[phase_id]
        phase = self._ensure_contract_phase(
            phases=phases,
            phase_by_id=phase_by_id,
            phase_section=phase_section,
            task=task,
        )
        phase_entries = entries_by_phase_id[phase_id]
        tdd_entries = [e for e in phase_entries if e.tdd]
        non_tdd_entries = [e for e in phase_entries if not e.tdd]

        # Aggregate phase test_intent from TDD entries before emitting the
        # phase-level [TEST] task so its description / validation can cite
        # cross-section behaviors.
        for entry in tdd_entries:
            section = section_by_id[entry.source_section]
            phase["test_intent"]["behaviors"].extend(
                item.prose for item in section.acceptance_items
            )
            phase["test_intent"]["suggested_test_files"] = sorted(
                set(phase["test_intent"]["suggested_test_files"])
                | set(_find_test_files(_contract_affected_files(section)))
            )

        phase_number = _contract_phase_number(
            phase_section.section_id if phase_section is not None else _DEFAULT_PHASE_ID
        )

        if tdd_entries:
            # Phase-level [TEST] at the start.
            phase_test_id = _stable_test_id(phase_id)
            phase_test_task = self._build_contract_phase_sandwich_task(
                kind="test",
                phase_id=phase_id,
                phase=phase,
                phase_number=phase_number,
                tdd_entries=tdd_entries,
                section_by_id=section_by_id,
            )
            tasks.append(phase_test_task)
            phase["task_ids"].append(phase_test_id)
            phase["tdd_sandwich_emitted"] = True
            if prior_phase_ref_id is not None:
                dependencies.append({"task_id": phase_test_id, "depends_on": prior_phase_ref_id})

            # Per-entry [IMPL] tasks.
            impl_ids: list[str] = []
            for entry in tdd_entries:
                section = section_by_id[entry.source_section]
                impl_task = self._build_contract_entry_work_task(
                    plan_doc=plan_doc,
                    entry=entry,
                    section=section,
                    phase_id=phase_id,
                    title_prefix="IMPL",
                )
                tasks.append(impl_task)
                phase["task_ids"].append(impl_task["id"])
                dependencies.append({"task_id": impl_task["id"], "depends_on": phase_test_id})
                entry_work_task_id[entry.source_section] = impl_task["id"]
                impl_ids.append(impl_task["id"])

            # Phase-level [REF] at the end.
            phase_ref_id = _stable_ref_id(phase_id)
            phase_ref_task = self._build_contract_phase_sandwich_task(
                kind="ref",
                phase_id=phase_id,
                phase=phase,
                phase_number=phase_number,
                tdd_entries=tdd_entries,
                section_by_id=section_by_id,
            )
            tasks.append(phase_ref_task)
            phase["task_ids"].append(phase_ref_id)
            for impl_id in impl_ids:
                dependencies.append({"task_id": phase_ref_id, "depends_on": impl_id})
            prior_phase_ref_id = phase_ref_id
        else:
            phase["tdd_sandwich_emitted"] = False

        # Non-TDD entries: emit a single task each (no prefix).
        for entry in non_tdd_entries:
            section = section_by_id[entry.source_section]
            single_task = self._build_contract_entry_work_task(
                plan_doc=plan_doc,
                entry=entry,
                section=section,
                phase_id=phase_id,
                title_prefix=None,
            )
            tasks.append(single_task)
            phase["task_ids"].append(single_task["id"])
            entry_work_task_id[entry.source_section] = single_task["id"]

    # Cross-deliverable dependencies — wire IMPL→IMPL or single→IMPL across
    # the graph using each entry's manifest depends_on list.
    for entry in plan_doc.manifest_entries:
        for dep_section in entry.depends_on:
            blocker = section_by_id.get(dep_section)
            blocker_entry = manifest_entry_by_section.get(dep_section)
            if blocker is None or blocker.kind is not Kind.deliverable or blocker_entry is None:
                raise ValueError(
                    f"manifest entry source_section={entry.source_section!r} depends on "
                    f"{dep_section!r}, which has no manifest entry"
                )
            dependencies.append(
                {
                    "task_id": entry_work_task_id[entry.source_section],
                    "depends_on": entry_work_task_id[dep_section],
                }
            )

    return {
        "version": 1,
        "parent_task_id": task.id,
        "plan_file": str(plan_doc.source_path),
        "phases": phases,
        "tasks": tasks,
        "dependencies": self._dedupe_dependencies(dependencies),
        "execution_groups": [],
        "deferrals": deferrals,
        "contract_plan": True,
        "plan_id": plan_id,
        "deliverable_count": len(plan_doc.manifest_entries),
    }


def _validate_contract_manifest(
    self: Any,
    plan_doc: PlanDocument,
    section_by_id: dict[str, PlanSection],
) -> dict[str, ManifestEntry]:
    manifest_entry_by_section = {entry.source_section: entry for entry in plan_doc.manifest_entries}
    for entry in plan_doc.manifest_entries:
        section = section_by_id.get(entry.source_section)
        if section is None or section.kind is not Kind.deliverable:
            raise ValueError(
                f"manifest entry source_section={entry.source_section!r} "
                "does not resolve to a kind: deliverable section"
            )

    deliverable_ids = {
        section.section_id for section in plan_doc.sections if section.kind is Kind.deliverable
    }
    orphan_deliverables = sorted(deliverable_ids - set(manifest_entry_by_section))
    if orphan_deliverables:
        raise ValueError(
            f"kind: deliverable sections without manifest entries: {', '.join(orphan_deliverables)}"
        )
    return manifest_entry_by_section


def _contract_deferrals(self: Any, plan_doc: PlanDocument) -> list[dict[str, Any]]:
    deferrals: list[dict[str, Any]] = []
    for section in plan_doc.sections:
        if section.kind is not Kind.deferred:
            continue
        record = _contract_deferral_record(section)
        if record is not None:
            deferrals.append(record)
    return deferrals


def _ensure_contract_phase(
    self: Any,
    *,
    phases: list[dict[str, Any]],
    phase_by_id: dict[str, dict[str, Any]],
    phase_section: PlanSection | None,
    task: Task,
) -> dict[str, Any]:
    phase_source_id = phase_section.section_id if phase_section is not None else _DEFAULT_PHASE_ID
    phase_id = _contract_phase_spec_id(phase_source_id)
    phase = phase_by_id.get(phase_id)
    if phase is not None:
        return phase

    phase_number = _contract_phase_number(phase_source_id)
    phase = {
        "id": phase_id,
        "title": phase_section.title if phase_section is not None else task.title,
        "summary": phase_section.title if phase_section is not None else task.description or "",
        "test_intent": {
            "summary": f"Write failing tests for Phase {phase_number}.",
            "behaviors": [],
            "suggested_test_files": [],
            "entry_criteria": ["Tests should fail before implementation begins."],
        },
        "task_ids": [],
        "tdd_sandwich_emitted": True,
    }
    phases.append(phase)
    phase_by_id[phase_id] = phase
    return phase


def _build_contract_phase_sandwich_task(
    self: Any,
    *,
    kind: str,
    phase_id: str,
    phase: dict[str, Any],
    phase_number: int,
    tdd_entries: list[ManifestEntry],
    section_by_id: dict[str, PlanSection],
) -> dict[str, Any]:
    """Build a phase-level ``[TEST]`` (kind=``"test"``) or ``[REF]`` (kind=``"ref"``) task.

    Aggregates labels, affected-file hints, and the assigned agent across
    the TDD entries so the phase wrapper carries the union of provenance.
    """
    is_test = kind == "test"
    title = (
        f"[TEST] Phase {phase_number}: Write failing tests"
        if is_test
        else f"[REF] Phase {phase_number}: Refactor with green tests"
    )
    description = (
        self._build_phase_test_description(phase, phase_number)
        if is_test
        else self._build_phase_refactor_description(phase, phase_number)
    )
    validation = (
        self._build_phase_test_validation(phase)
        if is_test
        else "All tests remain green after refactoring."
    )

    labels: set[str] = set()
    affected_files: set[str] = set()
    for entry in tdd_entries:
        labels.update(entry.labels)
        section = section_by_id[entry.source_section]
        section_files = _contract_affected_files(section)
        if is_test:
            affected_files.update(_find_test_files(section_files))
        else:
            affected_files.update(section_files)

    assigned_agent = tdd_entries[0].assigned_agent if tdd_entries else None

    return {
        "id": _stable_test_id(phase_id) if is_test else _stable_ref_id(phase_id),
        "phase_id": phase_id,
        "title": title,
        "description": description,
        "priority": 2,
        "task_type": "task",
        "category": "test" if is_test else "refactor",
        "validation": validation,
        "affected_files": sorted(affected_files),
        "labels": sorted(labels),
        "assigned_agent": assigned_agent,
        "additional_skills": [],
        "source_section_id": None,
    }


def _build_contract_entry_work_task(
    self: Any,
    *,
    plan_doc: PlanDocument,
    entry: ManifestEntry,
    section: PlanSection,
    phase_id: str,
    title_prefix: str | None,
) -> dict[str, Any]:
    """Build the per-entry work task — ``[IMPL]``-prefixed for TDD entries
    (``title_prefix="IMPL"``) or unprefixed for non-TDD entries
    (``title_prefix=None``)."""
    body = _contract_section_body(plan_doc, section)
    affected_files = _contract_affected_files(section)
    acceptance_lines = _contract_acceptance_lines(section)
    description = f"Plan section `{section.section_id}`.\n\n{body}".strip()
    if acceptance_lines:
        description = f"{description}\n\nAcceptance items:\n" + "\n".join(acceptance_lines)

    if title_prefix is None:
        task_id = _contract_single_task_id(section.section_id)
        title = entry.title
    else:
        _test_id, impl_id, _ref_id = _contract_task_ids(section.section_id)
        task_id = impl_id
        title = f"[{title_prefix}] {entry.title}"

    return {
        "id": task_id,
        "phase_id": phase_id,
        "title": title,
        "description": description,
        "priority": 2,
        "task_type": entry.task_type,
        "category": entry.category,
        "validation": entry.validation_criteria,
        "affected_files": affected_files,
        "labels": list(entry.labels),
        "assigned_agent": entry.assigned_agent,
        "additional_skills": [],
        "source_section_id": section.section_id,
    }


def _contract_phase_index(self: Any, plan_doc: PlanDocument) -> dict[str, PlanSection]:
    section_by_id = {section.section_id: section for section in plan_doc.sections}
    phase_by_section_id: dict[str, PlanSection] = {}
    for section in plan_doc.sections:
        current = section
        while current.parent_id is not None:
            parent = section_by_id.get(current.parent_id)
            if parent is None:
                break
            if _CONTRACT_PHASE_ID_RE.match(parent.section_id):
                phase_by_section_id[section.section_id] = parent
                break
            current = parent
    return phase_by_section_id


def _parse_contract_plan(self: Any, run: ExpansionRun, task: Task) -> PlanDocument | None:
    if not run.plan_file:
        return None
    plan_path = Path(run.plan_file)
    if not plan_path.is_absolute():
        repo_path = self._resolve_repo_path(task)
        if repo_path is not None:
            plan_path = repo_path / plan_path
    try:
        return parse_plan(plan_path, parse_mode="expansion")
    except (OSError, PlanParseError) as exc:
        self.run_manager.append_log(
            run.id,
            level="error",
            message="Plan file did not parse as Plan-Coverage Contract",
            extra={"plan_file": str(plan_path), "error": str(exc)},
        )
        raise ValueError(
            f"Plan file must conform to the Plan-Coverage Contract: {plan_path}"
        ) from exc


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


def validate_compiled_spec(self: Any, compiled_spec: dict[str, Any]) -> dict[str, Any]:
    """Validate compiled-spec structure and dependency integrity."""
    errors: list[str] = []
    tasks = compiled_spec.get("tasks") or []
    phases = compiled_spec.get("phases") or []
    dependencies = compiled_spec.get("dependencies") or []

    if not tasks:
        errors.append("Compiled spec contains no tasks")
    if not phases:
        errors.append("Compiled spec contains no phases")

    task_ids = [task["id"] for task in tasks if task.get("id")]
    phase_ids = [phase["id"] for phase in phases if phase.get("id")]

    if len(task_ids) != len(set(task_ids)):
        errors.append("Task IDs must be unique")
    if len(phase_ids) != len(set(phase_ids)):
        errors.append("Phase IDs must be unique")

    valid_task_ids = set(task_ids)
    valid_phase_ids = set(phase_ids)
    for task_item in tasks:
        if task_item.get("phase_id") not in valid_phase_ids:
            errors.append(
                f"Task {task_item.get('id')} references unknown phase {task_item.get('phase_id')}"
            )
        if not task_item.get("title"):
            errors.append(f"Task {task_item.get('id')} is missing a title")
        category = str(task_item.get("category", "code"))
        if category not in AUTOMATED_LEAF_CATEGORIES:
            errors.append(f"Task {task_item.get('id')} has unsupported category:{category}")

    for phase in phases:
        phase_task_ids = phase.get("task_ids") or []
        if not phase_task_ids:
            errors.append(f"Phase {phase.get('id')} has no task_ids")
        for stable_id in phase_task_ids:
            if stable_id not in valid_task_ids:
                errors.append(f"Phase {phase.get('id')} references unknown task {stable_id}")

    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in dependencies:
        task_id = edge.get("task_id")
        depends_on = edge.get("depends_on")
        if task_id not in valid_task_ids:
            errors.append(f"Dependency references unknown task {task_id}")
            continue
        if depends_on not in valid_task_ids:
            errors.append(f"Dependency {task_id} -> {depends_on} references unknown blocker")
            continue
        if task_id == depends_on:
            errors.append(f"Task {task_id} cannot depend on itself")
            continue
        adjacency[task_id].append(depends_on)

    visiting: set[str] = set()
    visited: set[str] = set()

    def _detect_cycle(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for blocker in adjacency.get(node, []):
            if _detect_cycle(blocker):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for task_id in valid_task_ids:
        if _detect_cycle(task_id):
            errors.append("Compiled spec dependency graph contains a cycle")
            break

    return {
        "valid": not errors,
        "errors": errors,
        "task_count": len(tasks),
        "phase_count": len(phases),
        "plan_file": compiled_spec.get("plan_file"),
    }


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

    skipped_stages = sorted(_skipped_stages(task))
    return {
        "task_id": task.id,
        "title": task.title,
        "description": task.description or "",
        "skipped_stages": skipped_stages,
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
        "dependencies": self._dedupe_dependencies(dependencies),
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


def _dedupe_dependencies(self: Any, dependencies: list[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate dependency edges while preserving order."""
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for edge in dependencies:
        key = (edge["task_id"], edge["depends_on"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"task_id": edge["task_id"], "depends_on": edge["depends_on"]})
    return deduped
