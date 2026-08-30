"""Typed pipeline loader: DB-first load, discovery, and revision-aware cache."""

from __future__ import annotations

import json
import logging
import threading
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.paths import get_global_workflows_dir
from gobby.storage.definitions.pipelines import (
    PipelineDefinitionManager,
    PipelineDefinitionRow,
)
from gobby.storage.definitions.revisions import (
    get_definitions_revision,
    register_revision_listener,
)
from gobby.utils.project_context import get_project_context
from gobby.utils.uuid_validation import parse_uuid_reference

from .loader_cache import (
    DiscoveredWorkflow,
    _CachedDiscovery,
    _CachedEntry,
    clear_cache,
)
from .loader_sync import PipelineLoaderSyncMixin
from .loader_validation import (
    _check_refs,
    _extract_step_refs,
    _validate_pipeline_references,
)
from .pipeline_models import PipelineDefinition

__all__ = ["PipelineLoader", "detect_override_conflict", "is_bundled_template"]

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

_LIVE_LOADERS: weakref.WeakSet[PipelineLoader] = weakref.WeakSet()
_LOADER_CACHE_LOCK = threading.Lock()
_LISTENER_REGISTERED = False


def _clear_pipeline_loader_caches() -> None:
    with _LOADER_CACHE_LOCK:
        for loader in tuple(_LIVE_LOADERS):
            loader.clear_cache()


def _ensure_revision_listener() -> None:
    global _LISTENER_REGISTERED
    if _LISTENER_REGISTERED:
        return
    register_revision_listener("pipelines", _clear_pipeline_loader_caches)
    _LISTENER_REGISTERED = True


def _row_tags(row: Any) -> set[str]:
    tags = getattr(row, "tags", None) or []
    return {str(tag).lower() for tag in tags}


def _payload_has_override(payload: Any) -> bool:
    if isinstance(payload, dict):
        return payload.get("override") is True
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(data, dict) and data.get("override") is True
    return False


def _definition_has_override_label(row: Any) -> bool:
    if getattr(row, "override", False) is True:
        return True
    tags = _row_tags(row)
    if "override" in tags or "override:true" in tags:
        return True
    return _payload_has_override(getattr(row, "definition_json", None)) or _payload_has_override(
        getattr(row, "definition", None)
    )


def is_bundled_template(row: Any) -> bool:
    """Return whether a definition row is a bundled Gobby template."""
    tags = _row_tags(row)
    source = str(getattr(row, "source", "")).lower()
    owner = str(getattr(row, "owner", "")).lower()
    return "gobby" in tags or source in {"gobby", "template"} or owner == "gobby"


# Alias used by the rules router (previously imported from loader.py).
_is_bundled_template = is_bundled_template


def detect_override_conflict(user_row: Any, bundled_row: Any | None) -> None:
    """Fail when a user definition shadows a bundled template without an override label."""
    if bundled_row is None or not is_bundled_template(bundled_row):
        return
    if _definition_has_override_label(user_row):
        return

    name = getattr(user_row, "name", "<unknown>")
    raise ValueError(
        f"Project workflow definition '{name}' conflicts with a bundled Gobby template. "
        "Add `override: true` to the project-local copy to make the override explicit."
    )


def _row_definition_dict(row: PipelineDefinitionRow) -> dict[str, Any]:
    payload = row.definition_json
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        loaded = json.loads(payload)
        if isinstance(loaded, dict):
            return loaded
    raise ValueError(f"Pipeline '{row.name}' has a non-object definition_json")


class PipelineLoader(PipelineLoaderSyncMixin):
    def __init__(self, db: HubDatabase | None = None) -> None:
        self.global_dirs = [get_global_workflows_dir()]
        self._cache: dict[str, _CachedEntry] = {}
        self._discovery_cache: dict[str, _CachedDiscovery] = {}
        self.db: HubDatabase | None = db
        self._def_manager: PipelineDefinitionManager | None = None
        _ensure_revision_listener()
        with _LOADER_CACHE_LOCK:
            _LIVE_LOADERS.add(self)

    @property
    def def_manager(self) -> PipelineDefinitionManager | None:
        """Lazy-init the typed pipeline definition manager."""
        if self._def_manager is None and self.db is not None:
            self._def_manager = PipelineDefinitionManager(self.db)
        return self._def_manager

    def _current_revision(self) -> int:
        return get_definitions_revision("pipelines")

    def _db_project_scope(self, project_path: Path | str | None, *, label: str) -> str | None:
        if project_path is None:
            return None
        parsed = parse_uuid_reference(project_path)
        if parsed is not None:
            return str(parsed)

        project_context = get_project_context(Path(project_path).expanduser())
        project_id = project_context.get("id") if project_context else None
        parsed = parse_uuid_reference(project_id)
        if parsed is not None:
            return str(parsed)

        logger.debug("Ignoring unresolved project scope for %s lookup: %s", label, project_path)
        return None

    def _load_from_db(
        self,
        name: str,
        project_id: str | None = None,
        _visited: set[str] | None = None,
    ) -> PipelineDefinition | None:
        if _visited is None:
            _visited = set()

        if name in _visited:
            logger.error("Circular 'extends' detected in DB pipelines: %s", name)
            raise ValueError(f"Circular 'extends' detected in DB pipelines: {name}")

        _visited.add(name)

        mgr = self.def_manager
        if mgr is None:
            return None
        row = mgr.get_by_name(name, project_id=project_id)
        if row is None:
            return None
        if project_id is not None and row.project_id is not None:
            detect_override_conflict(row, mgr.get_by_name(name, project_id=None))
        try:
            data = _row_definition_dict(row)
            data["name"] = row.name

            if "extends" in data:
                parent_name = data["extends"]
                parent_def = self._load_from_db(
                    parent_name, project_id=project_id, _visited=_visited
                )
                if parent_def:
                    data = self._merge_pipelines(parent_def.model_dump(), data)
                else:
                    logger.warning(
                        "Parent pipeline '%s' not found in DB for '%s'", parent_name, name
                    )

            data["enabled"] = row.enabled
            if row.version:
                data["version"] = row.version
            _validate_pipeline_references(data)
            return PipelineDefinition(**data)
        except Exception as e:
            logger.exception("Failed to parse DB pipeline '%s': %s", name, e)
            raise ValueError(f"Failed to parse DB pipeline '{name}': {e}") from e

    async def load_pipeline(
        self,
        name: str,
        project_path: Path | str | None = None,
        _inheritance_chain: list[str] | None = None,
    ) -> PipelineDefinition | None:
        """Load a pipeline by name. DB-only at runtime."""
        if _inheritance_chain is None:
            _inheritance_chain = []

        if name in _inheritance_chain:
            cycle_path = " -> ".join(_inheritance_chain + [name])
            logger.error("Circular pipeline inheritance detected: %s", cycle_path)
            raise ValueError(f"Circular pipeline inheritance detected: {cycle_path}")

        project_id = self._db_project_scope(project_path, label="pipeline")
        revision = self._current_revision()
        cache_key = f"pipeline:{project_id or 'global'}:{name}"
        if cache_key in self._cache:
            entry = self._cache[cache_key]
            if entry.revision == revision:
                return entry.definition

        visited = set(_inheritance_chain) if _inheritance_chain else set()
        db_definition = self._load_from_db(name, project_id=project_id, _visited=visited)
        if db_definition is not None:
            self._cache[cache_key] = _CachedEntry(definition=db_definition, revision=revision)
            return db_definition

        logger.debug("Pipeline '%s' not found in database", name)
        return None

    def _merge_pipelines(self, parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
        """Deep merge parent and child pipeline dicts. Child overrides parent."""
        merged = parent.copy()

        for key, value in child.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = self._merge_pipelines(merged[key], value)
            elif key in ("phases", "steps") and ("phases" in merged or "steps" in merged):
                parent_list = merged.get("phases") or merged.get("steps", [])
                merged_key = "phases" if "phases" in merged else "steps"
                merged[merged_key] = self._merge_steps(parent_list, value)
            else:
                merged[key] = value

        return merged

    def _merge_steps(self, parent_steps: list[Any], child_steps: list[Any]) -> list[Any]:
        """Merge step lists by step id or name."""
        key_field = "id" if (parent_steps and "id" in parent_steps[0]) else "name"
        if not parent_steps and child_steps:
            key_field = "id" if "id" in child_steps[0] else "name"

        parent_map: dict[str, dict[str, Any]] = {}
        for step in parent_steps:
            if key_field not in step:
                logger.warning("Skipping parent step without '%s' key", key_field)
                continue
            parent_map[step[key_field]] = dict(step)

        for child_step in child_steps:
            if key_field not in child_step:
                logger.warning("Skipping child step without '%s' key", key_field)
                continue
            name = child_step[key_field]
            if name in parent_map:
                parent_map[name].update(child_step)
            else:
                parent_map[name] = dict(child_step)

        return list(parent_map.values())

    async def discover_pipelines(
        self, project_path: Path | str | None = None
    ) -> list[DiscoveredWorkflow]:
        """Discover enabled pipelines from the typed table."""
        project_id = self._db_project_scope(project_path, label="pipeline discovery")
        revision = self._current_revision()
        cache_key = f"pipelines:{project_id}" if project_id else "pipelines:global"
        if cache_key in self._discovery_cache:
            cached = self._discovery_cache[cache_key]
            if cached.revision == revision:
                return cached.results

        discovered: dict[str, DiscoveredWorkflow] = {}
        mgr = self.def_manager
        if mgr is not None:
            try:
                rows = mgr.list_all(project_id=project_id, enabled=True)
            except Exception as e:
                logger.warning("Failed to list pipeline definitions: %s", e)
                rows = []
            for row in rows:
                try:
                    data = _row_definition_dict(row)
                    data["enabled"] = row.enabled
                    if row.version:
                        data["version"] = row.version
                    _validate_pipeline_references(data)
                    definition = PipelineDefinition(**data)
                    is_project = row.project_id is not None
                    existing = discovered.get(row.name)
                    if existing is not None and existing.is_project and not is_project:
                        continue
                    discovered[row.name] = DiscoveredWorkflow(
                        name=row.name,
                        definition=definition,
                        priority=getattr(definition, "priority", 100),
                        is_project=is_project,
                        path=Path(f"db://{row.id}"),
                    )
                except Exception as e:
                    logger.warning("Failed to parse DB pipeline '%s': %s", row.name, e)

        sorted_pipelines = sorted(
            discovered.values(),
            key=lambda item: (0 if item.is_project else 1, item.priority, item.name),
        )
        self._discovery_cache[cache_key] = _CachedDiscovery(
            results=sorted_pipelines, revision=revision
        )
        return sorted_pipelines

    def clear_cache(self) -> None:
        """Clear the pipeline definition and discovery caches."""
        clear_cache(self._cache, self._discovery_cache)

    async def validate_pipeline_for_agent(
        self,
        pipeline_name: str,
        project_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Validate that a named pipeline can be used as a spawn workflow."""
        try:
            pipeline = await self.load_pipeline(pipeline_name, project_id)
        except ValueError as e:
            return False, f"Failed to load pipeline '{pipeline_name}': {e}"
        if pipeline is None:
            return False, f"Pipeline '{pipeline_name}' not found"
        return True, None

    def _validate_pipeline_references(self, data: dict[str, Any]) -> None:
        _validate_pipeline_references(data)

    def _extract_step_refs(self, text: str) -> set[str]:
        return _extract_step_refs(text)

    def _check_refs(
        self,
        refs: set[str],
        valid_refs: set[str],
        all_step_ids: list[str],
        current_step: str,
        field_name: str,
    ) -> None:
        _check_refs(refs, valid_refs, all_step_ids, current_step, field_name)
