"""Shared services for task expansion runs."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from jinja2 import Environment, StrictUndefined

from gobby.config.app import DaemonConfig
from gobby.prompts.loader import PromptLoader
from gobby.prompts.models import parse_frontmatter
from gobby.storage.expansion_runs import ExpansionRun, LocalExpansionRunManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.commits import extract_mentioned_files
from gobby.utils.json_helpers import extract_json_object
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)

_TDD_CATEGORIES = frozenset({"code", "config"})
_BUNDLED_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "install" / "shared" / "prompts"


def _extract_phase_number(subtask: dict[str, Any]) -> int | None:
    """Extract phase number from '### Plan Section: N.N' in description."""
    desc = subtask.get("description", "")
    match = re.search(r"###\s+Plan Section:\s*(\d+)\.", desc)
    return int(match.group(1)) if match else None


def _extract_phase_from_title(subtask: dict[str, Any]) -> int | None:
    """Extract phase number from TDD-generated titles like '[TEST] Phase 2: ...'."""
    title = subtask.get("title", "")
    match = re.search(r"Phase\s+(\d+)", title)
    return int(match.group(1)) if match else None


def _get_subtask_phase(subtask: dict[str, Any]) -> int:
    """Get a phase number for a legacy subtask, or 0 when unphased."""
    return _extract_phase_number(subtask) or _extract_phase_from_title(subtask) or 0


_PHASE_HEADING_RE = re.compile(r"##\s+Phase\s+(\d+)\s*(?::|[\u2014\u2013-])\s*(.+)")
_PHASE_SECTION_RE = re.compile(
    r"^##\s+Phase\s+(\d+)\s*(?::|[\u2014\u2013-])\s*(.+?)$",
    flags=re.MULTILINE,
)


def _extract_phase_titles(description: str) -> dict[int, str]:
    """Extract phase titles from plan document content in task description.

    Accepts `## Phase N: Title`, `## Phase N — Title` (em-dash),
    `## Phase N – Title` (en-dash), and `## Phase N - Title` (ASCII hyphen).
    """
    titles: dict[int, str] = {}
    for match in _PHASE_HEADING_RE.finditer(description):
        titles[int(match.group(1))] = match.group(2).strip()
    return titles


def _extract_phase_sections(content: str) -> list[dict[str, Any]]:
    """Split plan markdown into ordered phase sections.

    Each section spans from its ``## Phase N: Name`` heading to (but not
    including) the next same-level phase heading or end-of-file. Accepts the
    same separator characters as :func:`_extract_phase_titles`.
    """
    matches = list(_PHASE_SECTION_RE.finditer(content))
    sections: list[dict[str, Any]] = []
    for i, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections.append(
            {
                "number": int(match.group(1)),
                "title": match.group(2).strip(),
                "body": content[body_start:body_end].strip(),
            }
        )
    return sections


def _prefix_spec_ids(spec: dict[str, Any], *, prefix: str) -> dict[str, Any]:
    """Prefix stable IDs in a compiled spec so sibling specs can merge without collision."""

    def pfx(value: str) -> str:
        return f"{prefix}{value}" if value and not value.startswith(prefix) else value

    phases = [
        {
            **phase,
            "id": pfx(phase["id"]),
            "task_ids": [pfx(tid) for tid in phase.get("task_ids") or []],
        }
        for phase in spec.get("phases") or []
    ]
    tasks = [
        {
            **task_item,
            "id": pfx(task_item["id"]),
            "phase_id": pfx(task_item["phase_id"]),
        }
        for task_item in spec.get("tasks") or []
    ]
    dependencies = [
        {"task_id": pfx(edge["task_id"]), "depends_on": pfx(edge["depends_on"])}
        for edge in spec.get("dependencies") or []
        if edge.get("task_id") and edge.get("depends_on")
    ]
    execution_groups = [
        {
            **group,
            "id": pfx(group["id"]) if group.get("id") else group.get("id"),
            "task_ids": [pfx(tid) for tid in group.get("task_ids") or []],
        }
        for group in spec.get("execution_groups") or []
    ]
    return {
        **spec,
        "phases": phases,
        "tasks": tasks,
        "dependencies": dependencies,
        "execution_groups": execution_groups,
    }


def _translate_deps(deps: list[int], old_to_new: dict[int, int]) -> list[int]:
    """Translate original dependency indices to new indices."""
    return [old_to_new[d] for d in deps if d in old_to_new]


def _apply_tdd_sandwich(subtasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap legacy phased subtask specs with deterministic TEST/REF bookends."""
    phase_groups: dict[int, list[int]] = {}
    for i, st in enumerate(subtasks):
        phase = _get_subtask_phase(st)
        phase_groups.setdefault(phase, []).append(i)

    if list(phase_groups.keys()) == [0]:
        phase_groups = {1: phase_groups[0]}
    elif 0 in phase_groups:
        max_phase = max(p for p in phase_groups if p > 0)
        phase_groups[max_phase + 1] = phase_groups.pop(0)

    sorted_phases = sorted(phase_groups.keys())

    new_subtasks: list[dict[str, Any]] = []
    old_to_new: dict[int, int] = {}
    phase_ref_idx: dict[int, int] = {}

    for phase_num in sorted_phases:
        orig_indices = phase_groups[phase_num]
        orig_set = set(orig_indices)

        has_tdd_tasks = any(subtasks[i].get("category") in _TDD_CATEGORIES for i in orig_indices)
        if not has_tdd_tasks:
            for orig_idx in orig_indices:
                new_idx = len(new_subtasks)
                old_to_new[orig_idx] = new_idx
                st = dict(subtasks[orig_idx])
                st["depends_on"] = _translate_deps(st.get("depends_on", []), old_to_new)
                new_subtasks.append(st)
            continue

        cross_deps: set[int] = set()
        for orig_idx in orig_indices:
            if subtasks[orig_idx].get("category") not in _TDD_CATEGORIES:
                continue
            for dep in subtasks[orig_idx].get("depends_on", []):
                if dep not in orig_set and dep in old_to_new:
                    dep_phase = _get_subtask_phase(subtasks[dep])
                    if dep_phase != 0 and dep_phase in phase_ref_idx:
                        cross_deps.add(phase_ref_idx[dep_phase])
                    else:
                        cross_deps.add(old_to_new[dep])

        impl_titles = [
            subtasks[i]["title"]
            for i in orig_indices
            if subtasks[i].get("category") in _TDD_CATEGORIES
        ]

        test_new_idx = len(new_subtasks)
        new_subtasks.append(
            {
                "title": f"[TEST] Phase {phase_num}: Write failing tests",
                "category": "test",
                "description": (
                    f"Write failing tests for Phase {phase_num} implementation tasks:\n\n"
                    + "\n".join(f"- {title}" for title in impl_titles)
                    + "\n\nTests should cover the expected behavior described in each task."
                ),
                "validation": (
                    "Tests exist and fail with expected assertion errors "
                    "(not import or syntax errors)"
                ),
                "priority": subtasks[orig_indices[0]].get("priority", 2),
                "depends_on": sorted(cross_deps),
            }
        )

        impl_start = len(new_subtasks)
        for i, orig_idx in enumerate(orig_indices):
            old_to_new[orig_idx] = impl_start + i
            new_subtasks.append(dict(subtasks[orig_idx]))

        for orig_idx in orig_indices:
            st = new_subtasks[old_to_new[orig_idx]]
            if st.get("category") in _TDD_CATEGORIES:
                new_deps = [test_new_idx]
                for dep in st.get("depends_on", []):
                    if dep in orig_set and dep in old_to_new:
                        new_deps.append(old_to_new[dep])
                st["depends_on"] = new_deps
            else:
                st["depends_on"] = _translate_deps(st.get("depends_on", []), old_to_new)

        ref_new_idx = len(new_subtasks)
        phase_ref_idx[phase_num] = ref_new_idx
        new_subtasks.append(
            {
                "title": f"[REF] Phase {phase_num}: Refactor with green tests",
                "category": "refactor",
                "description": (
                    f"Refactor Phase {phase_num} while keeping all tests green.\n\n"
                    "Review for duplication, naming, complexity, and clarity."
                ),
                "validation": "All tests pass and the implementation remains behaviorally correct.",
                "priority": subtasks[orig_indices[0]].get("priority", 2),
                "depends_on": [
                    old_to_new[i]
                    for i in orig_indices
                    if subtasks[i].get("category") in _TDD_CATEGORIES
                ],
            }
        )

    return new_subtasks


def _slugify(value: str, *, fallback: str) -> str:
    """Build a stable ASCII-ish identifier slug."""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or fallback


def _stable_test_id(phase_id: str) -> str:
    return f"{phase_id}::__test"


def _stable_ref_id(phase_id: str) -> str:
    return f"{phase_id}::__ref"


def _strip_frontmatter(markdown: str) -> str:
    """Strip YAML frontmatter from a bundled prompt file."""
    _frontmatter, body = parse_frontmatter(markdown)
    return body


def _read_text_if_exists(path: Path, *, max_chars: int | None = None) -> str | None:
    """Read UTF-8 text from a file when it exists."""
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if max_chars is not None:
        return text[:max_chars]
    return text


def _find_test_files(files: Iterable[str]) -> list[str]:
    """Extract likely test file paths from a file list."""
    test_files: list[str] = []
    for file_path in files:
        lowered = file_path.lower()
        if "/tests/" in lowered or lowered.startswith("tests/") or "test_" in lowered:
            test_files.append(file_path)
    return sorted(set(test_files))


def _render_template(template_str: str, context: dict[str, Any]) -> str:
    """Render a Jinja template string."""
    env = Environment(autoescape=False, undefined=StrictUndefined)
    env.filters["default"] = lambda value, default="": default if value is None else value
    return str(env.from_string(template_str).render(**context))


class ExpansionService:
    """Compile and apply expansion runs."""

    def __init__(
        self,
        *,
        task_manager: LocalTaskManager,
        llm_service: Any,
        config: DaemonConfig | None = None,
        run_manager: LocalExpansionRunManager | None = None,
    ) -> None:
        self.task_manager = task_manager
        self.db = task_manager.db
        self.llm_service = llm_service
        self.config = config
        self.run_manager = run_manager or LocalExpansionRunManager(self.db)
        self.dep_manager = TaskDependencyManager(self.db)
        self.af_manager = TaskAffectedFileManager(self.db)
        self.project_manager = LocalProjectManager(self.db)
        self.prompt_loader = PromptLoader(db=self.db)

    def validate_plan_file(self, plan_path: Path) -> dict[str, Any]:
        """Validate a plan file exists and identify phase headings."""
        if not plan_path.exists():
            return {"valid": False, "errors": [f"Plan file not found: {plan_path}"]}
        content = _read_text_if_exists(plan_path)
        if content is None:
            return {"valid": False, "errors": [f"Could not read plan file: {plan_path}"]}
        phase_titles = _extract_phase_titles(content)
        return {
            "valid": True,
            "path": str(plan_path),
            "phase_count": len(phase_titles),
            "phases": phase_titles,
        }

    async def compile_run(self, run_id: str) -> ExpansionRun:
        """Compile an expansion run into a normalized compiled spec."""
        run = self.run_manager.get(run_id)
        if run is None:
            raise ValueError(f"Expansion run {run_id} not found")
        task = self.task_manager.get_task(run.parent_task_id)
        if task is None:
            raise ValueError(f"Parent task {run.parent_task_id} not found")
        self.run_manager.start(run_id)
        self.run_manager.append_log(run_id, level="info", message="Starting expansion compile")

        phase_sections = self._load_phase_sections(run.plan_file, task)
        if len(phase_sections) >= 2:
            self.run_manager.append_log(
                run_id,
                level="info",
                message=f"Detected {len(phase_sections)} phases; compiling per-phase",
            )
            compiled_spec = await self._compile_multi_phase(run, task, phase_sections)
        else:
            raw_spec = await self._generate_raw_spec(run, task)
            compiled_spec = self.normalize_compiled_spec(
                raw_spec, task=task, plan_file=run.plan_file
            )
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
        self,
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
        if auto_apply:
            return self.apply_run(run.id, session_id=session_id)
        return run

    def apply_run(self, run_id: str, *, session_id: str | None) -> ExpansionRun:
        """Apply a compiled expansion spec to the task tree."""
        run = self.run_manager.get(run_id)
        if run is None:
            raise ValueError(f"Expansion run {run_id} not found")
        if not run.compiled_spec:
            raise ValueError(f"Expansion run {run_id} has no compiled spec")

        task = self.task_manager.get_task(run.parent_task_id)
        if task is None:
            raise ValueError(f"Parent task {run.parent_task_id} not found")

        spec = run.compiled_spec
        validation = self.validate_compiled_spec(spec)
        if not validation["valid"]:
            errors = "; ".join(validation["errors"])
            raise ValueError(f"Cannot apply invalid compiled spec: {errors}")

        phase_list = spec["phases"]
        tasks = spec["tasks"]
        dependency_edges = spec["dependencies"]
        multi_phase = len(phase_list) > 1
        phase_index_by_id = {phase["id"]: i + 1 for i, phase in enumerate(phase_list)}
        phase_has_tdd = {
            phase["id"]: any(
                task_item.get("category") in _TDD_CATEGORIES
                for task_item in tasks
                if task_item["phase_id"] == phase["id"]
            )
            for phase in phase_list
        }

        epic_validation = "All expanded child tasks must be completed."
        plan_ref_block = ""
        if run.plan_file:
            plan_ref_block = (
                f"> **Plan reference:** `{run.plan_file}`\n"
                "> Your task description below is authoritative; the plan is supporting context only.\n\n"
            )

        phase_parent_map: dict[str, str] = {}
        created_task_map: dict[str, str] = {}
        phase_child_ids: dict[str, list[str]] = defaultdict(list)
        task_label_map = {
            task_item["id"]: (
                [f"parallel:{task_item['execution_group']}"]
                if task_item.get("execution_group")
                else None
            )
            for task_item in tasks
        }

        with self.db.transaction():
            self.run_manager.mark_applying(run_id)
            self.run_manager.append_log(run_id, level="info", message="Applying compiled expansion")

            # Create phase subepics first for genuinely multi-phase expansions.
            if multi_phase:
                for phase in phase_list:
                    result = self.task_manager.create_task_with_decomposition(
                        project_id=task.project_id,
                        title=phase["title"],
                        task_type="epic",
                        parent_task_id=task.id,
                        category="planning",
                        validation_criteria=epic_validation,
                        created_in_session_id=session_id,
                        description=phase.get("summary"),
                    )
                    phase_parent_map[phase["id"]] = result["task"]["id"]
            else:
                phase_parent_map = {phase["id"]: task.id for phase in phase_list}

            tasks_by_phase: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for task_item in tasks:
                tasks_by_phase[task_item["phase_id"]].append(task_item)

            for phase in phase_list:
                phase_id = phase["id"]
                parent_id = phase_parent_map[phase_id]
                phase_number = phase_index_by_id[phase_id]
                tdd_enabled = phase_has_tdd[phase_id]

                if tdd_enabled:
                    test_description = self._build_phase_test_description(phase, phase_number)
                    test_validation = self._build_phase_test_validation(phase)
                    test_result = self.task_manager.create_task_with_decomposition(
                        project_id=task.project_id,
                        title=f"[TEST] Phase {phase_number}: Write failing tests",
                        description=f"{plan_ref_block}{test_description}"
                        if plan_ref_block
                        else test_description,
                        priority=self._phase_priority(tasks_by_phase[phase_id]),
                        task_type="task",
                        parent_task_id=parent_id,
                        category="test",
                        validation_criteria=test_validation,
                        created_in_session_id=session_id,
                    )
                    created_task_map[_stable_test_id(phase_id)] = test_result["task"]["id"]
                    phase_child_ids[phase_id].append(test_result["task"]["id"])
                    suggested_tests = phase.get("test_intent", {}).get("suggested_test_files") or []
                    if suggested_tests:
                        self.af_manager.set_files(
                            test_result["task"]["id"], suggested_tests, "expansion"
                        )

                for task_item in tasks_by_phase[phase_id]:
                    raw_description = task_item.get("description") or ""
                    description = (
                        f"{plan_ref_block}{raw_description}" if plan_ref_block else raw_description
                    )
                    create_result = self.task_manager.create_task_with_decomposition(
                        project_id=task.project_id,
                        title=task_item["title"],
                        description=description or None,
                        priority=task_item.get("priority", 2),
                        task_type=task_item.get("task_type", "task"),
                        parent_task_id=parent_id,
                        category=task_item.get("category"),
                        validation_criteria=task_item.get("validation"),
                        created_in_session_id=session_id,
                        labels=task_label_map.get(task_item["id"]),
                    )
                    created_id = create_result["task"]["id"]
                    created_task_map[task_item["id"]] = created_id
                    phase_child_ids[phase_id].append(created_id)
                    affected_files = task_item.get("affected_files") or []
                    if affected_files:
                        self.af_manager.set_files(created_id, affected_files, "expansion")

                if tdd_enabled:
                    ref_description = self._build_phase_refactor_description(phase, phase_number)
                    ref_result = self.task_manager.create_task_with_decomposition(
                        project_id=task.project_id,
                        title=f"[REF] Phase {phase_number}: Refactor with green tests",
                        description=f"{plan_ref_block}{ref_description}"
                        if plan_ref_block
                        else ref_description,
                        priority=self._phase_priority(tasks_by_phase[phase_id]),
                        task_type="task",
                        parent_task_id=parent_id,
                        category="refactor",
                        validation_criteria="All tests remain green after refactoring.",
                        created_in_session_id=session_id,
                    )
                    created_task_map[_stable_ref_id(phase_id)] = ref_result["task"]["id"]
                    phase_child_ids[phase_id].append(ref_result["task"]["id"])

            deps_by_task: dict[str, list[str]] = defaultdict(list)
            external_phase_deps: dict[str, set[str]] = defaultdict(set)
            for edge in dependency_edges:
                deps_by_task[edge["task_id"]].append(edge["depends_on"])

            for task_item in tasks:
                phase_id = task_item["phase_id"]
                stable_id = task_item["id"]
                created_id = created_task_map[stable_id]
                blockers = deps_by_task.get(stable_id, [])
                is_tdd_task = (
                    task_item.get("category") in _TDD_CATEGORIES and phase_has_tdd[phase_id]
                )
                if is_tdd_task:
                    # All implementation tasks in a TDD phase depend on the phase's failing-test task.
                    self._add_dependency(created_id, created_task_map[_stable_test_id(phase_id)])
                for blocker_id in blockers:
                    blocker_task = next((item for item in tasks if item["id"] == blocker_id), None)
                    if blocker_task is None:
                        continue
                    blocker_phase_id = blocker_task["phase_id"]
                    if is_tdd_task and blocker_phase_id != phase_id:
                        external_phase_deps[phase_id].add(
                            self._external_blocker_id(blocker_task, phase_has_tdd)
                        )
                        continue
                    blocker_created = self._resolve_created_blocker(
                        blocker_id,
                        tasks_by_id={item["id"]: item for item in tasks},
                        created_task_map=created_task_map,
                        phase_has_tdd=phase_has_tdd,
                    )
                    if blocker_created:
                        self._add_dependency(created_id, blocker_created)

            for phase in phase_list:
                phase_id = phase["id"]
                if phase_has_tdd[phase_id]:
                    test_id = created_task_map[_stable_test_id(phase_id)]
                    for blocker_stable in sorted(external_phase_deps.get(phase_id, set())):
                        blocker_created = created_task_map.get(blocker_stable)
                        if blocker_created:
                            self._add_dependency(test_id, blocker_created)
                    ref_id = created_task_map[_stable_ref_id(phase_id)]
                    for task_item in tasks_by_phase[phase_id]:
                        if task_item.get("category") in _TDD_CATEGORIES:
                            self._add_dependency(ref_id, created_task_map[task_item["id"]])

            if multi_phase:
                for phase in phase_list:
                    subepic_id = phase_parent_map[phase["id"]]
                    for child_id in phase_child_ids.get(phase["id"], []):
                        self._add_dependency(subepic_id, child_id)
                    self._add_dependency(task.id, subepic_id)
            else:
                for child_id in phase_child_ids.get(phase_list[0]["id"], []):
                    self._add_dependency(task.id, child_id)

            created_ids = list(dict.fromkeys(created_task_map.values()))
            run = self.run_manager.save_apply_result(
                run_id,
                task_id_map=created_task_map,
                created_task_ids=created_ids,
                checkpoints={
                    "apply_validation": self.validate_applied_run(
                        run_id, compiled_spec=spec, task_id_map=created_task_map
                    )
                },
                completed=True,
            )
        self.run_manager.append_log(
            run_id,
            level="info",
            message="Expansion apply completed",
            extra={"created_count": len(created_ids), "multi_phase": multi_phase},
        )
        if run is None:
            raise RuntimeError(f"Expansion run {run_id} disappeared after apply")
        return run

    def validate_applied_run(
        self,
        run_id: str,
        *,
        compiled_spec: dict[str, Any] | None = None,
        task_id_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Validate that an applied run created the expected task mapping."""
        run = self.run_manager.get(run_id)
        spec = compiled_spec or (run.compiled_spec if run else None)
        mapping = task_id_map or (run.task_id_map if run else None)
        errors: list[str] = []
        if spec is None:
            return {"valid": False, "errors": ["Expansion run has no compiled spec"]}
        if mapping is None:
            return {"valid": False, "errors": ["Expansion run has no applied task mapping"]}

        for task_item in spec["tasks"]:
            stable_id = task_item["id"]
            if stable_id not in mapping:
                errors.append(f"Missing created task mapping for {stable_id}")
                continue
            try:
                self.task_manager.get_task(mapping[stable_id])
            except Exception:
                errors.append(f"Created task {mapping[stable_id]} for {stable_id} no longer exists")

        return {"valid": not errors, "errors": errors}

    def normalize_compiled_spec(
        self,
        raw_spec: dict[str, Any],
        *,
        task: Task,
        plan_file: str | None,
    ) -> dict[str, Any]:
        """Normalize raw LLM output into the compiled expansion schema."""
        expansion_config = self._get_expansion_config()
        max_subtasks = expansion_config.max_subtasks if expansion_config else 15

        if "phases" in raw_spec and "tasks" in raw_spec:
            normalized = self._normalize_native_compiled_spec(
                raw_spec, task=task, plan_file=plan_file
            )
        elif "subtasks" in raw_spec:
            normalized = self._normalize_legacy_subtask_spec(
                raw_spec, task=task, plan_file=plan_file
            )
        else:
            raise ValueError("Expansion compiler must return either {phases,tasks} or {subtasks}")

        if len(normalized["tasks"]) > max_subtasks:
            raise ValueError(
                f"Compiled spec exceeds max_subtasks ({len(normalized['tasks'])} > {max_subtasks})"
            )
        return normalized

    def validate_compiled_spec(self, compiled_spec: dict[str, Any]) -> dict[str, Any]:
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

    async def _generate_raw_spec(self, run: ExpansionRun, task: Task) -> dict[str, Any]:
        """Call the configured LLM and return raw JSON output."""
        prompt_context = self._build_prompt_context(run, task)
        return await self._invoke_llm_compile(run, prompt_context)

    async def _generate_raw_spec_for_phase(
        self,
        run: ExpansionRun,
        task: Task,
        phase_section: dict[str, Any],
    ) -> dict[str, Any]:
        """Call the LLM scoped to a single plan-phase section."""
        prompt_context = self._build_prompt_context(
            run,
            task,
            plan_content_override=phase_section["body"],
            single_phase_mode=True,
            phase_title=phase_section["title"],
            phase_number=phase_section["number"],
        )
        return await self._invoke_llm_compile(
            run, prompt_context, phase_number=phase_section["number"]
        )

    async def _invoke_llm_compile(
        self,
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
        provider_name = run.provider or (
            expansion_config.provider if expansion_config else "claude"
        )
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

    async def _compile_multi_phase(
        self,
        run: ExpansionRun,
        task: Task,
        phase_sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compile a multi-phase plan by issuing one LLM call per phase, then merging."""
        expansion_config = self._get_expansion_config()
        max_subtasks = expansion_config.max_subtasks if expansion_config else 15

        merged_phases: list[dict[str, Any]] = []
        merged_tasks: list[dict[str, Any]] = []
        merged_deps: list[dict[str, str]] = []
        merged_exec_groups: list[dict[str, Any]] = []

        for section in phase_sections:
            self.run_manager.append_log(
                run.id,
                level="info",
                message=f"Compiling phase {section['number']}: {section['title']}",
            )
            raw = await self._generate_raw_spec_for_phase(run, task, section)
            if not (raw.get("tasks") or []):
                raise ValueError(f"Phase {section['number']} spec produced no tasks")
            normalized = self._normalize_native_compiled_spec(
                raw, task=task, plan_file=run.plan_file
            )
            if len(normalized["tasks"]) > max_subtasks:
                raise ValueError(
                    f"Phase {section['number']} spec exceeds max_subtasks "
                    f"({len(normalized['tasks'])} > {max_subtasks})"
                )
            prefixed = _prefix_spec_ids(normalized, prefix=f"phase-{section['number']}-")
            merged_phases.extend(prefixed["phases"])
            merged_tasks.extend(prefixed["tasks"])
            merged_deps.extend(prefixed["dependencies"])
            merged_exec_groups.extend(prefixed.get("execution_groups") or [])

        if not merged_tasks:
            raise ValueError("Multi-phase compile produced no tasks")

        return {
            "version": 1,
            "parent_task_id": task.id,
            "plan_file": run.plan_file,
            "phases": merged_phases,
            "tasks": merged_tasks,
            "dependencies": self._dedupe_dependencies(merged_deps),
            "execution_groups": merged_exec_groups,
        }

    def _build_prompt_context(
        self,
        run: ExpansionRun,
        task: Task,
        *,
        plan_content_override: str | None = None,
        single_phase_mode: bool = False,
        phase_title: str = "",
        phase_number: int = 0,
    ) -> dict[str, Any]:
        """Build prompt context for expansion compilation.

        When ``plan_content_override`` is provided, it replaces the on-disk
        plan content — used by the per-phase compile path so each LLM call
        sees only its own phase body.
        """
        repo_path = self._resolve_repo_path(task)
        if plan_content_override is not None:
            plan_content: str | None = plan_content_override
        else:
            plan_content = self._load_plan_content(run.plan_file, repo_path)
        verification = self._get_verification_commands(repo_path)
        file_context = self._build_file_context(task, repo_path, plan_content)

        verification_lines = []
        for name, command in verification.items():
            verification_lines.append(f"- `{name}`: `{command}`")
        verification_str = (
            "\n".join(verification_lines)
            if verification_lines
            else "- No verification commands configured."
        )

        research_sections: list[str] = [f"Project verification commands:\n{verification_str}"]
        if plan_content:
            plan_label = (
                f"Plan file ({run.plan_file}) — Phase {phase_number}: {phase_title}"
                if single_phase_mode
                else f"Plan file ({run.plan_file})"
            )
            research_sections.append(f"{plan_label}:\n{plan_content[:12000]}")
        if file_context:
            research_sections.append(file_context)

        return {
            "task_id": task.id,
            "title": task.title,
            "description": task.description or "",
            "context_str": "\n\n".join(research_sections),
            "research_str": file_context or "No repository files were selected for context.",
            "plan_file": run.plan_file or "",
            "single_phase_mode": single_phase_mode,
            "phase_title": phase_title,
            "phase_number": phase_number,
        }

    def _normalize_native_compiled_spec(
        self,
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
        for i, task_item in enumerate(raw_tasks):
            phase_id = task_item.get("phase_id") or normalized_phases[0]["id"]
            stable_id = task_item.get("id") or f"task-{i + 1:03d}"
            affected_files = list(task_item.get("affected_files") or [])
            normalized_task = {
                "id": stable_id,
                "phase_id": phase_id,
                "title": task_item.get("title") or f"Task {i + 1}",
                "description": task_item.get("description") or "",
                "priority": int(task_item.get("priority", 2)),
                "task_type": task_item.get("task_type", "task"),
                "category": task_item.get("category", "code"),
                "validation": task_item.get("validation") or task_item.get("validation_criteria"),
                "affected_files": affected_files,
                "execution_group": task_item.get("execution_group")
                or task_item.get("parallel_group"),
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

    def _normalize_legacy_subtask_spec(
        self,
        raw_spec: dict[str, Any],
        *,
        task: Task,
        plan_file: str | None,
    ) -> dict[str, Any]:
        """Convert the legacy `subtasks` array shape into a compiled spec."""
        legacy_subtasks = list(raw_spec.get("subtasks") or [])
        if not legacy_subtasks:
            raise ValueError("Legacy expansion spec contains no subtasks")

        phase_titles = _extract_phase_titles(task.description or "")
        raw_phase_map: dict[int, list[int]] = defaultdict(list)
        for i, subtask in enumerate(legacy_subtasks):
            raw_phase_map[_get_subtask_phase(subtask)].append(i)
        if list(raw_phase_map.keys()) == [0]:
            raw_phase_map = {1: raw_phase_map[0]}
        elif 0 in raw_phase_map:
            max_phase = max(num for num in raw_phase_map if num > 0)
            raw_phase_map[max_phase + 1] = raw_phase_map.pop(0)

        normalized_tasks: list[dict[str, Any]] = []
        dependencies: list[dict[str, str]] = []
        execution_group_index: dict[str, list[str]] = defaultdict(list)
        index_to_id: dict[int, str] = {}
        phases: list[dict[str, Any]] = []
        phase_num_to_id: dict[int, str] = {}

        for phase_num in sorted(raw_phase_map.keys()):
            phase_id = f"phase-{phase_num}"
            phase_num_to_id[phase_num] = phase_id
            phase_indices = raw_phase_map[phase_num]
            phase_tasks = [legacy_subtasks[i] for i in phase_indices]
            phase_title = phase_titles.get(phase_num, f"Phase {phase_num}")
            phase_test_intent = self._build_legacy_phase_test_intent(phase_tasks, phase_num)
            phase_task_ids: list[str] = []
            for local_index, global_index in enumerate(phase_indices):
                subtask = legacy_subtasks[global_index]
                stable_id = f"{phase_id}-task-{local_index + 1}"
                index_to_id[global_index] = stable_id
                phase_task_ids.append(stable_id)
                affected_files = list(subtask.get("affected_files") or [])
                normalized_tasks.append(
                    {
                        "id": stable_id,
                        "phase_id": phase_id,
                        "title": subtask.get("title") or f"Task {global_index + 1}",
                        "description": subtask.get("description") or "",
                        "priority": int(subtask.get("priority", 2)),
                        "task_type": subtask.get("task_type", "task"),
                        "category": subtask.get("category", "code"),
                        "validation": subtask.get("validation")
                        or subtask.get("validation_criteria"),
                        "affected_files": affected_files,
                        "execution_group": subtask.get("parallel_group"),
                    }
                )
                if subtask.get("parallel_group"):
                    execution_group_index[subtask["parallel_group"]].append(stable_id)
            phases.append(
                {
                    "id": phase_id,
                    "title": phase_title,
                    "summary": phase_title,
                    "test_intent": phase_test_intent,
                    "task_ids": phase_task_ids,
                }
            )

        for source_index, subtask in enumerate(legacy_subtasks):
            task_id = index_to_id[source_index]
            for blocker_index in subtask.get("depends_on") or []:
                blocker_id = index_to_id.get(blocker_index)
                if blocker_id:
                    dependencies.append({"task_id": task_id, "depends_on": blocker_id})

        execution_groups = [
            {"id": group_name, "mode": "parallel", "task_ids": task_ids}
            for group_name, task_ids in execution_group_index.items()
        ]

        return {
            "version": 1,
            "parent_task_id": task.id,
            "plan_file": plan_file or raw_spec.get("plan_file"),
            "phases": phases,
            "tasks": normalized_tasks,
            "dependencies": self._dedupe_dependencies(dependencies),
            "execution_groups": execution_groups,
        }

    def _build_legacy_phase_test_intent(
        self,
        phase_tasks: list[dict[str, Any]],
        phase_num: int,
    ) -> dict[str, Any]:
        """Derive explicit phase test metadata from legacy subtask output."""
        impl_titles = [
            task.get("title", "") for task in phase_tasks if task.get("category") in _TDD_CATEGORIES
        ]
        affected_files = [
            file_path for task in phase_tasks for file_path in (task.get("affected_files") or [])
        ]
        return {
            "summary": f"Verify Phase {phase_num} implementation behavior",
            "behaviors": [title for title in impl_titles if title],
            "suggested_test_files": _find_test_files(affected_files),
            "entry_criteria": ["Tests should fail before implementation begins."],
        }

    def _render_prompt(self, path: str, context: dict[str, Any]) -> str:
        """Render a prompt from DB-backed prompts with a bundled-file fallback."""
        try:
            return self.prompt_loader.render(path, context)
        except FileNotFoundError:
            prompt_file = _BUNDLED_PROMPTS_DIR / f"{path}.md"
            raw_content = _read_text_if_exists(prompt_file)
            if raw_content is None:
                raise
            return _render_template(_strip_frontmatter(raw_content), context)

    def _resolve_repo_path(self, task: Task) -> Path | None:
        """Resolve the repository path for the task's project."""
        project = self.project_manager.get(task.project_id)
        if project and project.repo_path:
            return Path(project.repo_path)
        project_ctx = get_project_context()
        if project_ctx and project_ctx.get("project_path"):
            return Path(project_ctx["project_path"])
        return None

    def _load_plan_content(
        self,
        plan_file: str | None,
        repo_path: Path | None,
        *,
        max_chars: int | None = 20000,
    ) -> str | None:
        """Load a plan file relative to the repo when provided."""
        if not plan_file:
            return None
        plan_path = Path(plan_file)
        if not plan_path.is_absolute() and repo_path is not None:
            plan_path = repo_path / plan_file
        return _read_text_if_exists(plan_path, max_chars=max_chars)

    def _load_phase_sections(self, plan_file: str | None, task: Task) -> list[dict[str, Any]]:
        """Load a plan file and split it into phase sections.

        Returns ``[]`` when ``plan_file`` is unset, unreadable, or contains no
        ``## Phase N`` headings. Reads the full file (no char cap) so phase
        splitting works on large plans; each section's body is still
        truncated in ``_build_prompt_context`` before reaching the LLM.
        """
        if not plan_file:
            return []
        repo_path = self._resolve_repo_path(task)
        content = self._load_plan_content(plan_file, repo_path, max_chars=None)
        if not content:
            return []
        return _extract_phase_sections(content)

    def _get_verification_commands(self, repo_path: Path | None) -> dict[str, str]:
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

    def _build_file_context(
        self, task: Task, repo_path: Path | None, plan_content: str | None
    ) -> str:
        """Build a focused repository context block for expansion prompts."""
        if repo_path is None:
            return ""
        task_payload = {
            "title": task.title,
            "description": task.description or "",
            "validation_criteria": task.validation_criteria,
        }
        mentioned_files = extract_mentioned_files(task_payload)
        if plan_content:
            plan_payload = {"title": "", "description": plan_content, "validation_criteria": None}
            mentioned_files.extend(extract_mentioned_files(plan_payload))

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

    def _get_expansion_config(self) -> Any | None:
        """Return task expansion config when available."""
        if self.config is None:
            return None
        return self.config.get_gobby_tasks_config().expansion

    def _phase_priority(self, phase_tasks: list[dict[str, Any]]) -> int:
        """Use the highest-priority task in a phase as the sandwich task priority."""
        priorities = [int(task_item.get("priority", 2)) for task_item in phase_tasks]
        return min(priorities) if priorities else 2

    def _build_phase_test_description(self, phase: dict[str, Any], phase_number: int) -> str:
        """Create deterministic test-task description from explicit phase metadata."""
        test_intent = phase.get("test_intent") or {}
        lines = [test_intent.get("summary") or f"Write failing tests for Phase {phase_number}."]
        behaviors = list(test_intent.get("behaviors") or [])
        if behaviors:
            lines.append("")
            lines.append("Behaviors to verify:")
            lines.extend(f"- {behavior}" for behavior in behaviors)
        suggested_test_files = list(test_intent.get("suggested_test_files") or [])
        if suggested_test_files:
            lines.append("")
            lines.append("Suggested test files:")
            lines.extend(f"- {file_path}" for file_path in suggested_test_files)
        entry_criteria = list(test_intent.get("entry_criteria") or [])
        if entry_criteria:
            lines.append("")
            lines.append("Entry criteria:")
            lines.extend(f"- {criterion}" for criterion in entry_criteria)
        return "\n".join(lines)

    def _build_phase_test_validation(self, phase: dict[str, Any]) -> str:
        """Create deterministic test-task validation text."""
        test_intent = phase.get("test_intent") or {}
        behaviors = list(test_intent.get("behaviors") or [])
        if behaviors:
            return (
                "Failing tests exist for the phase behaviors and fail for expected reasons: "
                + "; ".join(behaviors)
            )
        return "Failing tests exist for the phase behavior and fail for expected reasons."

    def _build_phase_refactor_description(self, phase: dict[str, Any], phase_number: int) -> str:
        """Create deterministic refactor-task description from phase metadata."""
        summary = phase.get("summary") or phase.get("title") or f"Phase {phase_number}"
        return (
            f"Refactor {summary} while keeping the new tests green.\n\n"
            "Review for duplication, naming, dead code, and unnecessarily complex flows."
        )

    def _external_blocker_id(
        self, blocker_task: dict[str, Any], phase_has_tdd: dict[str, bool]
    ) -> str:
        """Map an external blocker to the effective created task that should gate downstream work."""
        blocker_phase_id = blocker_task["phase_id"]
        if phase_has_tdd.get(blocker_phase_id) and blocker_task.get("category") in _TDD_CATEGORIES:
            return _stable_ref_id(blocker_phase_id)
        return cast(str, blocker_task["id"])

    def _resolve_created_blocker(
        self,
        blocker_id: str,
        *,
        tasks_by_id: dict[str, dict[str, Any]],
        created_task_map: dict[str, str],
        phase_has_tdd: dict[str, bool],
    ) -> str | None:
        """Resolve a stable blocker ID to the created task that should enforce it."""
        blocker_task = tasks_by_id.get(blocker_id)
        if blocker_task is None:
            return None
        phase_id = blocker_task["phase_id"]
        if phase_has_tdd.get(phase_id) and blocker_task.get("category") in _TDD_CATEGORIES:
            return created_task_map.get(blocker_id)
        return created_task_map.get(blocker_id)

    def _add_dependency(self, task_id: str, depends_on: str) -> None:
        """Best-effort dependency creation that ignores duplicates."""
        try:
            self.dep_manager.add_dependency(
                task_id=task_id, depends_on=depends_on, dep_type="blocks"
            )
        except ValueError:
            pass

    def _dedupe_dependencies(self, dependencies: list[dict[str, str]]) -> list[dict[str, str]]:
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
