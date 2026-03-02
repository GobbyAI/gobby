import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .definitions import PipelineDefinition, WorkflowDefinition
from .loader_cache import (
    DiscoveredWorkflow,
    _CachedDiscovery,
    _CachedEntry,
    clear_cache,
)
from .loader_discovery import (
    discover_pipeline_workflows,
    discover_workflows,
)
from .loader_sync import WorkflowLoaderSyncMixin
from .loader_validation import (
    _check_refs,
    _extract_step_refs,
    _validate_pipeline_references,
)

__all__ = ["WorkflowLoader"]

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

logger = logging.getLogger(__name__)


class WorkflowLoader(WorkflowLoaderSyncMixin):
    def __init__(
        self,
        db: "DatabaseProtocol | None" = None,
        # Legacy parameters kept for backward compatibility with tests
        workflow_dirs: list[Path] | None = None,
        bundled_dir: Path | None = None,
    ):
        # Legacy directory fields — unused at runtime but kept so tests that
        # pass workflow_dirs= still instantiate without errors.
        self.global_dirs = workflow_dirs or [Path.home() / ".gobby" / "workflows"]
        self._bundled_dir = bundled_dir
        self._cache: dict[str, _CachedEntry] = {}
        # Cache for discovered workflows per project path
        self._discovery_cache: dict[str, _CachedDiscovery] = {}
        # Database for DB-first workflow lookup (the only runtime source)
        self.db: DatabaseProtocol | None = db
        self._def_manager: LocalWorkflowDefinitionManager | None = None

    @property
    def def_manager(self) -> "LocalWorkflowDefinitionManager | None":
        """Lazy-init the workflow definition manager."""
        if self._def_manager is None and self.db is not None:
            from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

            self._def_manager = LocalWorkflowDefinitionManager(self.db)
        return self._def_manager

    def _load_from_db(
        self,
        name: str,
        project_id: str | None = None,
        _visited: set[str] | None = None,
    ) -> WorkflowDefinition | PipelineDefinition | None:
        """Try to load a workflow definition from the database.

        Returns the parsed definition if found in DB, None otherwise.

        Args:
            name: Workflow name to load.
            project_id: Optional project scope.
            _visited: Set of already-visited names for cycle detection in
                ``extends`` chains.  Callers should NOT pass this directly;
                it is managed internally and seeded from the caller's
                ``_inheritance_chain`` when available.
        """
        if _visited is None:
            _visited = set()

        if name in _visited:
            logger.error(f"Circular 'extends' detected in DB workflows: {name}")
            raise ValueError(f"Circular 'extends' detected in DB workflows: {name}")

        _visited.add(name)

        mgr = self.def_manager
        if mgr is None:
            return None
        row = mgr.get_by_name(name, project_id=project_id)
        if row is None:
            return None
        try:
            data = json.loads(row.definition_json)

            # Resolve extends from DB
            if "extends" in data:
                parent_name = data["extends"]
                parent_def = self._load_from_db(
                    parent_name, project_id=project_id, _visited=_visited
                )
                if parent_def:
                    data = self._merge_workflows(parent_def.model_dump(), data)
                else:
                    logger.warning(f"Parent workflow '{parent_name}' not found in DB for '{name}'")

            if row.workflow_type == "pipeline" or data.get("type") == "pipeline":
                self._validate_pipeline_references(data)
                return PipelineDefinition(**data)
            else:
                return WorkflowDefinition(**data)
        except Exception as e:
            logger.error(f"Failed to parse DB workflow '{name}': {e}", exc_info=True)
            return None

    async def load_workflow(
        self,
        name: str,
        project_path: Path | str | None = None,
        _inheritance_chain: list[str] | None = None,
    ) -> WorkflowDefinition | PipelineDefinition | None:
        """
        Load a workflow by name.

        DB-only at runtime.  YAML files are imported to the database
        during ``gobby install``.

        Qualified names (agent:workflow) are resolved by loading the inline
        workflow from the agent definition.

        Args:
            name: Workflow name, or qualified name (agent:workflow)
            project_path: Optional project directory for scoped lookup.
            _inheritance_chain: Internal parameter for cycle detection.

        Raises:
            ValueError: If circular inheritance is detected or pipeline
                        references are invalid.
        """
        # Cycle detection
        if _inheritance_chain is None:
            _inheritance_chain = []

        if name in _inheritance_chain:
            cycle_path = " -> ".join(_inheritance_chain + [name])
            logger.error(f"Circular workflow inheritance detected: {cycle_path}")
            raise ValueError(f"Circular workflow inheritance detected: {cycle_path}")

        # Cache check
        cache_key = f"{project_path or 'global'}:{name}"
        if cache_key in self._cache:
            entry = self._cache[cache_key]
            return entry.definition

        # DB lookup (the only runtime source)
        project_id = str(project_path) if project_path else None
        visited = set(_inheritance_chain) if _inheritance_chain else set()
        db_definition = self._load_from_db(name, project_id=project_id, _visited=visited)
        if db_definition is not None:
            self._cache[cache_key] = _CachedEntry(definition=db_definition, path=None, mtime=0.0)
            return db_definition

        logger.debug(f"Workflow '{name}' not found in database")
        return None

    async def load_pipeline(
        self,
        name: str,
        project_path: Path | str | None = None,
        _inheritance_chain: list[str] | None = None,
    ) -> PipelineDefinition | None:
        """
        Load a pipeline workflow by name.
        Only returns workflows with type='pipeline'.

        DB-only at runtime.

        Args:
            name: Pipeline name
            project_path: Optional project directory for scoped lookup.
            _inheritance_chain: Internal parameter for cycle detection.

        Returns:
            PipelineDefinition if found and type is 'pipeline', None otherwise.
        """
        if _inheritance_chain is None:
            _inheritance_chain = []

        if name in _inheritance_chain:
            cycle_path = " -> ".join(_inheritance_chain + [name])
            logger.error(f"Circular pipeline inheritance detected: {cycle_path}")
            raise ValueError(f"Circular pipeline inheritance detected: {cycle_path}")

        # Cache check
        cache_key = f"pipeline:{project_path or 'global'}:{name}"
        if cache_key in self._cache:
            entry = self._cache[cache_key]
            if isinstance(entry.definition, PipelineDefinition):
                return entry.definition
            return None

        # DB lookup
        project_id = str(project_path) if project_path else None
        visited = set(_inheritance_chain) if _inheritance_chain else set()
        db_definition = self._load_from_db(name, project_id=project_id, _visited=visited)
        if db_definition is not None:
            if isinstance(db_definition, PipelineDefinition):
                self._cache[cache_key] = _CachedEntry(
                    definition=db_definition, path=None, mtime=0.0
                )
                return db_definition
            logger.debug(f"'{name}' is not a pipeline (type={getattr(db_definition, 'type', '?')})")
            return None

        logger.debug(f"Pipeline '{name}' not found in database")
        return None

    def _validate_pipeline_references(self, data: dict[str, Any]) -> None:
        """Validate that all $step_id.output references in a pipeline refer to earlier steps."""
        _validate_pipeline_references(data)

    def _extract_step_refs(self, text: str) -> set[str]:
        """Extract step IDs from $step_id.output patterns in text."""
        return _extract_step_refs(text)

    def _check_refs(
        self,
        refs: set[str],
        valid_refs: set[str],
        all_step_ids: list[str],
        current_step: str,
        field_name: str,
    ) -> None:
        """Check that all references are valid."""
        _check_refs(refs, valid_refs, all_step_ids, current_step, field_name)

    def _merge_workflows(self, parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
        """
        Deep merge parent and child workflow dicts.
        Child overrides parent.
        """
        merged = parent.copy()

        for key, value in child.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = self._merge_workflows(merged[key], value)
            elif key in ("phases", "steps") and ("phases" in merged or "steps" in merged):
                parent_list = merged.get("phases") or merged.get("steps", [])
                merged_key = "phases" if "phases" in merged else "steps"
                merged[merged_key] = self._merge_steps(parent_list, value)
            else:
                merged[key] = value

        return merged

    def _merge_steps(self, parent_steps: list[Any], child_steps: list[Any]) -> list[Any]:
        """
        Merge step lists by step name or id.
        Supports both workflow steps (name key) and pipeline steps (id key).
        """
        key_field = "id" if (parent_steps and "id" in parent_steps[0]) else "name"
        if not parent_steps and child_steps:
            key_field = "id" if "id" in child_steps[0] else "name"

        parent_map: dict[str, dict[str, Any]] = {}
        for s in parent_steps:
            if key_field not in s:
                logger.warning(f"Skipping parent step without '{key_field}' key")
                continue
            parent_map[s[key_field]] = dict(s)

        for child_step in child_steps:
            if key_field not in child_step:
                logger.warning(f"Skipping child step without '{key_field}' key")
                continue
            name = child_step[key_field]
            if name in parent_map:
                parent_map[name].update(child_step)
            else:
                parent_map[name] = dict(child_step)

        return list(parent_map.values())

    async def discover_workflows(
        self, project_path: Path | str | None = None
    ) -> list[DiscoveredWorkflow]:
        """Discover all workflows from the database."""
        return await discover_workflows(self, project_path)

    async def discover_pipeline_workflows(
        self, project_path: Path | str | None = None
    ) -> list[DiscoveredWorkflow]:
        """Discover all pipeline workflows from the database."""
        return await discover_pipeline_workflows(self, project_path)

    def clear_cache(self) -> None:
        """
        Clear the workflow definitions and discovery cache.
        """
        clear_cache(self._cache, self._discovery_cache)

    def register_inline_workflow(
        self,
        name: str,
        data: dict[str, Any],
        project_path: Path | str | None = None,
    ) -> WorkflowDefinition | PipelineDefinition:
        """
        Register an inline workflow definition in the cache.

        Inline workflows are embedded in agent definitions and registered
        at spawn time with qualified names like "agent:workflow".

        Note: Inline workflows are NOT written to disk. Child agents can load
        them directly from agent definitions via load_workflow() which handles
        qualified names (agent:workflow) by parsing the agent YAML.
        """
        cache_key = f"{project_path or 'global'}:{name}"

        if cache_key in self._cache:
            entry = self._cache[cache_key]
            if isinstance(entry.definition, (WorkflowDefinition, PipelineDefinition)):
                return entry.definition

        if "name" not in data or data.get("name") is None:
            data["name"] = name

        try:
            if data.get("type") == "pipeline":
                self._validate_pipeline_references(data)
                definition: WorkflowDefinition | PipelineDefinition = PipelineDefinition(**data)
            else:
                definition = WorkflowDefinition(**data)

            self._cache[cache_key] = _CachedEntry(definition=definition, path=None, mtime=0.0)

            wf_type = data.get("type", "step")
            logger.debug(f"Registered inline {wf_type} workflow '{name}'")
            return definition

        except Exception as e:
            logger.error(f"Failed to register inline workflow '{name}': {e}")
            raise ValueError(f"Invalid inline workflow '{name}': {e}") from e

    async def validate_workflow_for_agent(
        self,
        workflow_name: str,
        project_path: Path | str | None = None,
    ) -> tuple[bool, str | None]:
        """
        Validate that a workflow can be used for agent spawning.

        Lifecycle workflows run automatically via hooks and cannot be
        explicitly activated for agents. Only step workflows are valid.
        """
        try:
            workflow = await self.load_workflow(workflow_name, project_path=project_path)
        except ValueError as e:
            return False, f"Failed to load workflow '{workflow_name}': {e}"

        if not workflow:
            return True, None

        if isinstance(workflow, WorkflowDefinition) and workflow.enabled:
            return False, (
                f"Cannot use always-on workflow '{workflow_name}' for agent spawning. "
                f"Always-on workflows run automatically on events. "
                f"Use an on-demand workflow (enabled: false) instead."
            )

        return True, None
