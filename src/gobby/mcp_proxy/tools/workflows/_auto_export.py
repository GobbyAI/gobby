"""Auto-export workflow definitions to YAML template files.

Provides helpers used by the MCP rule/agent/pipeline/variable tools to
auto-export project-scoped definitions to .gobby/workflows/ on disk,
enabling persistence across DB resets.
"""

import json
import logging
from pathlib import Path

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import json_array_contains_condition
from gobby.storage.workflow_definitions import WorkflowDefinitionRow
from gobby.workflows.template_writer import (
    delete_template_file,
    write_agent_template,
    write_pipeline_template,
    write_rule_template,
    write_variable_template,
)

logger = logging.getLogger(__name__)


def has_gobby_name_collision(db: HubDatabase, name: str) -> bool:
    """Check if a name collides with a bundled gobby definition.

    Args:
        db: Database connection
        name: Definition name to check

    Returns:
        True if a gobby-tagged definition with this name exists
    """
    tag_condition, tag_params = json_array_contains_condition(db, "tags", "gobby")
    row = db.fetchone(
        "SELECT id FROM workflow_definitions "
        f"WHERE name = ? AND {tag_condition} "
        "AND deleted_at IS NULL",
        (name, *tag_params),
    )
    return row is not None


def auto_export_definition(
    row: WorkflowDefinitionRow,
    project_path: Path | None = None,
    *,
    make_global: bool = False,
) -> Path | None:
    """Auto-export a workflow definition to YAML on disk.

    Skips export in dev mode. Writes to project (.gobby/workflows/) or
    global (~/.gobby/workflows/) location based on make_global flag.

    Args:
        row: The workflow definition row to export
        project_path: Project root path (for project-scoped export)
        make_global: If True, export to global ~/.gobby/workflows/ instead

    Returns:
        Path to written file, or None if skipped
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

    output_dir = dirs.get(row.workflow_type)
    if not output_dir:
        logger.debug(f"Unknown workflow_type for export: {row.workflow_type}")
        return None

    definition = json.loads(row.definition_json)
    tags = row.tags or ["user"]

    if row.workflow_type == "rule":
        return write_rule_template(
            name=row.name,
            definition=definition,
            output_dir=output_dir,
            tags=tags,
        )
    elif row.workflow_type == "pipeline":
        return write_pipeline_template(
            name=row.name,
            definition=definition,
            output_dir=output_dir,
        )
    elif row.workflow_type == "agent":
        return write_agent_template(
            name=row.name,
            definition=definition,
            output_dir=output_dir,
        )
    elif row.workflow_type == "variable":
        return write_variable_template(
            name=row.name,
            definition=definition,
            output_dir=output_dir,
        )

    return None


def auto_delete_definition(
    name: str,
    workflow_type: str,
    project_path: Path | None = None,
    *,
    delete_global: bool = False,
) -> bool:
    """Delete a YAML template file when a definition is deleted.

    Args:
        name: Definition name
        workflow_type: Type (rule, pipeline, agent, variable)
        project_path: Project root path
        delete_global: Also delete from global directory

    Returns:
        True if any file was deleted
    """
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
        output_dir = dirs.get(workflow_type)
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
        output_dir = global_dirs.get(workflow_type)
        if output_dir:
            deleted = delete_template_file(name, output_dir) or deleted

    return deleted
