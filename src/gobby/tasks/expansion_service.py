"""Public facade for task expansion runs."""

from __future__ import annotations

from typing import Any

from gobby.config.app import DaemonConfig
from gobby.plans.parser import PlanDocument
from gobby.prompts.loader import PromptLoader
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.tasks.categories import AUTOMATED_LEAF_CATEGORIES, DEVELOPMENT_FORWARD_LEAF_CATEGORIES
from gobby.tasks.expansion import _apply, _compile, _contract, _reset, _validate
from gobby.tasks.expansion._common import (
    _contract_single_task_id,
    list_agent_definitions,
)

__all__ = [
    "AUTOMATED_LEAF_CATEGORIES",
    "DEVELOPMENT_FORWARD_LEAF_CATEGORIES",
    "ExpansionService",
    "_contract_single_task_id",
    "compile_plan_to_spec",
    "list_agent_definitions",
]


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
        self.definition_manager = LocalWorkflowDefinitionManager(self.db)
        self.prompt_loader = PromptLoader(db=self.db)
        self._agent_definition_cache: dict[str | None, list[dict[str, Any]]] = {}

    validate_plan_file = _validate.validate_plan_file
    compile_plan_to_spec = _contract.compile_plan_to_spec
    _validate_contract_manifest = _contract._validate_contract_manifest
    _contract_deferrals = _contract._contract_deferrals
    _ensure_contract_phase = _contract._ensure_contract_phase
    _build_contract_entry_work_task = _contract._build_contract_entry_work_task
    _contract_phase_index = _contract._contract_phase_index
    _parse_contract_plan = _contract._parse_contract_plan
    compile_run = _compile.compile_run
    compile_and_apply_run = _compile.compile_and_apply_run
    normalize_compiled_spec = _compile.normalize_compiled_spec
    _list_agent_definitions_for_selection = _compile._list_agent_definitions_for_selection
    validate_compiled_spec = _validate.validate_compiled_spec
    _generate_raw_spec = _compile._generate_raw_spec
    _invoke_llm_compile = _compile._invoke_llm_compile
    _build_prompt_context = _compile._build_prompt_context
    _normalize_native_compiled_spec = _compile._normalize_native_compiled_spec
    _render_prompt = _compile._render_prompt
    _resolve_repo_path = _compile._resolve_repo_path
    _get_verification_commands = _compile._get_verification_commands
    _build_file_context = _compile._build_file_context

    _complete_dev_only_run = _apply._complete_dev_only_run
    apply_run = _apply.apply_run
    validate_applied_run = _apply.validate_applied_run
    _get_expansion_config = _apply._get_expansion_config
    _add_dependency = _apply._add_dependency

    reset_expansion_output = _reset.reset_expansion_output
    find_existing_expansion_output = _reset.find_existing_expansion_output
    find_apply_blocking_expansion_output = _reset.find_apply_blocking_expansion_output
    _complete_parent_expansion_stage_if_current = _reset.complete_parent_expansion_stage_if_current


def compile_plan_to_spec(
    service: ExpansionService, plan_doc: PlanDocument, task: Task
) -> dict[str, Any]:
    """Compile a Plan-Coverage Contract through the public expansion facade."""
    return service.compile_plan_to_spec(plan_doc, task)
