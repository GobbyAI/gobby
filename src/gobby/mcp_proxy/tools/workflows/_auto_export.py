"""Auto-export domain definitions to YAML template files.

Helpers used by the MCP rule/agent/pipeline/variable tools to persist
project-scoped definitions under .gobby/workflows/.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal, Protocol

from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.template_writer import (
    delete_template_file,
    write_agent_template,
    write_pipeline_template,
    write_rule_template,
    write_variable_template,
)

logger = logging.getLogger(__name__)

DefinitionKind = Literal["rule", "variable", "agent", "pipeline"]

_KIND_TABLES: dict[DefinitionKind, type[Any]] = {
    "rule": RuleDefinitionManager,
    "variable": SessionVariableDefaultManager,
    "agent": AgentDefinitionManager,
    "pipeline": PipelineDefinitionManager,
}


class _Exportable(Protocol):
    name: str
    tags: list[str] | None
    definition_json: str | dict[str, Any]


def _definition_payload(row: _Exportable) -> dict[str, Any]:
    raw = row.definition_json
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {}
    return dict(raw)


def has_gobby_name_collision(db: HubDatabase, name: str, kind: DefinitionKind) -> bool:
    """Return True if a gobby-tagged definition of *kind* already uses *name*."""
    manager = _KIND_TABLES[kind](db)
    return any(row.name == name and "gobby" in (row.tags or []) for row in manager.list_all())


def auto_export_definition(
    row: _Exportable,
    project_path: Path | None = None,
    *,
    kind: DefinitionKind,
    make_global: bool = False,
) -> Path | None:
    """Auto-export a domain definition to YAML on disk.

    Skips export in dev mode. Writes to project or global type directories.
    """
    from gobby.utils.dev import is_dev_mode

    if project_path and is_dev_mode(project_path):
        logger.debug("Skipping auto-export in dev mode")
        return None

    if make_global:
        from gobby.paths import (
            get_global_agents_dir,
            get_global_pipelines_dir,
            get_global_rules_dir,
            get_global_variables_dir,
        )

        dirs = {
            "rule": get_global_rules_dir(),
            "pipeline": get_global_pipelines_dir(),
            "agent": get_global_agents_dir(),
            "variable": get_global_variables_dir(),
        }
    elif project_path:
        from gobby.paths import (
            get_project_agents_dir,
            get_project_pipelines_dir,
            get_project_rules_dir,
            get_project_variables_dir,
        )

        dirs = {
            "rule": get_project_rules_dir(project_path),
            "pipeline": get_project_pipelines_dir(project_path),
            "agent": get_project_agents_dir(project_path),
            "variable": get_project_variables_dir(project_path),
        }
    else:
        logger.debug("No project path and make_global=False, skipping export")
        return None

    output_dir = dirs.get(kind)
    if not output_dir:
        logger.debug("Unknown kind for export: %s", kind)
        return None

    definition = _definition_payload(row)
    tags = row.tags or ["user"]

    if kind == "rule":
        return write_rule_template(
            name=row.name,
            definition=definition,
            output_dir=output_dir,
            tags=tags,
        )
    if kind == "pipeline":
        return write_pipeline_template(
            name=row.name,
            definition=definition,
            output_dir=output_dir,
        )
    if kind == "agent":
        return write_agent_template(
            name=row.name,
            definition=definition,
            output_dir=output_dir,
        )
    return write_variable_template(
        name=row.name,
        definition=definition,
        output_dir=output_dir,
    )


def auto_delete_definition(
    name: str,
    project_path: Path | None = None,
    *,
    kind: DefinitionKind,
    delete_global: bool = False,
) -> bool:
    """Delete a YAML template file when a definition is deleted."""
    deleted = False

    if project_path:
        from gobby.paths import (
            get_project_agents_dir,
            get_project_pipelines_dir,
            get_project_rules_dir,
            get_project_variables_dir,
        )

        dirs = {
            "rule": get_project_rules_dir(project_path),
            "pipeline": get_project_pipelines_dir(project_path),
            "agent": get_project_agents_dir(project_path),
            "variable": get_project_variables_dir(project_path),
        }
        output_dir = dirs.get(kind)
        if output_dir:
            deleted = delete_template_file(name, output_dir) or deleted

    if delete_global:
        from gobby.paths import (
            get_global_agents_dir,
            get_global_pipelines_dir,
            get_global_rules_dir,
            get_global_variables_dir,
        )

        global_dirs = {
            "rule": get_global_rules_dir(),
            "pipeline": get_global_pipelines_dir(),
            "agent": get_global_agents_dir(),
            "variable": get_global_variables_dir(),
        }
        output_dir = global_dirs.get(kind)
        if output_dir:
            deleted = delete_template_file(name, output_dir) or deleted

    return deleted
