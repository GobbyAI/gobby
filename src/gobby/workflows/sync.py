"""Workflow definition synchronization for bundled workflows.

This module provides sync_bundled_workflows() which loads workflow definitions
from the bundled install/shared/workflows/ directory and syncs them to the database.
"""

import json
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from gobby.storage.database import DatabaseProtocol
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import PipelineDefinition, RuleDefinitionBody, WorkflowDefinition

__all__ = [
    "get_bundled_rules_path",
    "get_bundled_workflows_path",
    "get_bundled_variables_path",
    "sync_bundled_rules",
    "sync_bundled_workflows",
    "sync_bundled_variables",
]

logger = logging.getLogger(__name__)

VALID_WORKFLOW_TYPES = {"rule", "variable", "agent", "pipeline"}


def get_bundled_rules_path() -> Path:
    """Get the path to bundled rules directory.

    Returns:
        Path to src/gobby/install/shared/rules/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "rules"


def get_bundled_workflows_path() -> Path:
    """Get the path to bundled workflows directory.

    Returns:
        Path to src/gobby/install/shared/workflows/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "workflows"


def sync_bundled_workflows(db: DatabaseProtocol) -> dict[str, Any]:
    """Sync bundled workflow definitions from install/shared/workflows/ to the database.

    This function:
    1. Walks all .yaml files in the bundled workflows directory (skips deprecated/)
    2. Parses each and validates it has a 'name' field
    3. Creates new records or updates changed content (idempotent)
    4. All records are created with source='template' and project_id=None

    Args:
        db: Database connection

    Returns:
        Dict with success status and counts
    """
    workflows_path = get_bundled_workflows_path()

    result: dict[str, Any] = {
        "success": True,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    if not workflows_path.exists():
        logger.warning("Bundled workflows path not found", extra={"path": str(workflows_path)})
        result["errors"].append(f"Workflows path not found: {workflows_path}")
        return result

    manager = LocalWorkflowDefinitionManager(db)

    for yaml_file in sorted(workflows_path.glob("*.yaml")):
        try:
            raw_content = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)

            if not isinstance(data, dict):
                logger.warning("Skipping non-dict YAML file", extra={"workflow": str(yaml_file)})
                continue

            if "name" not in data:
                logger.warning(
                    "Skipping YAML without 'name' field", extra={"workflow": str(yaml_file)}
                )
                continue

            # Validate against Pydantic schema before any DB operations
            schema_cls = (
                PipelineDefinition if data.get("type") == "pipeline" else WorkflowDefinition
            )
            try:
                schema_cls(**data)
            except ValidationError as ve:
                logger.warning(
                    "Skipping invalid workflow",
                    extra={"workflow": str(yaml_file), "error": str(ve)},
                )
                continue

            name = data["name"]
            definition_json = json.dumps(data)

            # Derive metadata from the YAML content
            yaml_type = data.get("type", "")
            workflow_type = yaml_type if yaml_type in VALID_WORKFLOW_TYPES else "pipeline"
            description = data.get("description", "")
            version = str(data.get("version", "1.0"))
            enabled = bool(data.get("enabled", False))
            priority = data.get("priority", 100)
            sources_list = data.get("sources")

            # Check if workflow already exists (global scope, including soft-deleted)
            existing = manager.get_by_name(name, include_deleted=True, include_templates=True)

            if existing is not None:
                # If soft-deleted template, restore it so templates are always available
                if existing.deleted_at is not None:
                    if existing.source == "template":
                        manager.restore(existing.id)
                        manager.update(
                            existing.id,
                            name=name,
                            definition_json=definition_json,
                            workflow_type=workflow_type,
                            project_id=None,
                            description=description,
                            version=version,
                            enabled=False,
                            priority=priority,
                            sources=sources_list,
                            source="template",
                            tags=["gobby"],
                        )
                        logger.info(
                            "Restored soft-deleted bundled workflow",
                            extra={"workflow": name},
                        )
                        result["updated"] += 1
                    else:
                        logger.debug(
                            "Non-template workflow is soft-deleted, skipping sync",
                            extra={"workflow": name},
                        )
                        result["skipped"] += 1
                    continue

                if existing.source == "template":
                    # Compare definition_json content to detect changes
                    if existing.definition_json == definition_json:
                        logger.debug(
                            "Workflow already up to date, skipping", extra={"workflow": name}
                        )
                        result["skipped"] += 1
                    else:
                        # Atomic in-place update (preserves id and user's enabled toggle)
                        manager.update(
                            existing.id,
                            name=name,
                            definition_json=definition_json,
                            workflow_type=workflow_type,
                            project_id=None,
                            description=description,
                            version=version,
                            enabled=existing.enabled,
                            priority=priority,
                            sources=sources_list,
                            source="template",
                            tags=["gobby"],
                        )
                        # Propagate definition changes to installed copy
                        _propagate_to_installed(manager, name, definition_json)
                        logger.info("Updated bundled workflow definition", extra={"workflow": name})
                        result["updated"] += 1
                else:
                    # Non-template workflow with same name shadows the template.
                    # Look up the actual template row and update it if changed,
                    # then propagate to the installed copy.
                    template_row = manager.db.fetchone(
                        "SELECT * FROM workflow_definitions "
                        "WHERE name = ? AND source = 'template' AND deleted_at IS NULL",
                        (name,),
                    )
                    if template_row:
                        from gobby.storage.workflow_definitions import WorkflowDefinitionRow

                        tpl = WorkflowDefinitionRow.from_row(template_row)
                        if tpl.definition_json != definition_json:
                            manager.update(
                                tpl.id,
                                definition_json=definition_json,
                                description=description,
                                version=version,
                                enabled=tpl.enabled,
                                priority=priority,
                                sources=sources_list,
                                source="template",
                                tags=["gobby"],
                            )
                            _propagate_to_installed(manager, name, definition_json)
                            logger.info(
                                "Updated shadowed workflow template and propagated",
                                extra={"workflow": name},
                            )
                            result["updated"] += 1
                        else:
                            result["skipped"] += 1
                    else:
                        logger.debug(
                            "Workflow exists with non-template source, no template to update",
                            extra={"workflow": name, "source": existing.source},
                        )
                        result["skipped"] += 1
                continue

            # Create the workflow definition in the database
            manager.create(
                name=name,
                definition_json=definition_json,
                workflow_type=workflow_type,
                project_id=None,
                description=description,
                version=version,
                enabled=enabled,
                priority=priority,
                sources=sources_list,
                source="template",
                tags=["gobby"],
            )
            logger.info("Synced bundled workflow definition", extra={"workflow": name})
            result["synced"] += 1

        except Exception as e:
            error_msg = f"Failed to sync workflow definition '{yaml_file}': {e}"
            logger.error(
                "Failed to sync workflow definition",
                extra={"workflow": str(yaml_file), "error": str(e)},
            )
            result["errors"].append(error_msg)

    # Orphan cleanup: soft-delete template workflows whose YAML was removed.
    # Collects names from disk, then marks any DB rows with source='template'
    # that aren't in the set.  This handles workflows moved to deprecated/.
    on_disk = set()
    for yf in sorted(workflows_path.glob("*.yaml")):
        try:
            d = yaml.safe_load(yf.read_text(encoding="utf-8"))
            if isinstance(d, dict) and "name" in d:
                on_disk.add(d["name"])
        except Exception:
            pass  # Already logged above during main loop

    orphan_rows = db.fetchall(
        "SELECT id, name FROM workflow_definitions "
        "WHERE source = 'template' AND workflow_type IN ('workflow', 'pipeline') AND deleted_at IS NULL",
    )
    result["orphaned"] = 0
    for row in orphan_rows:
        if row["name"] not in on_disk:
            manager.delete(row["id"])
            logger.info("Soft-deleted orphaned bundled workflow", extra={"workflow": row["name"]})
            result["orphaned"] += 1

    _ensure_gobby_tag_on_installed(manager, "workflow")
    _ensure_gobby_tag_on_installed(manager, "pipeline")

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.info(
        "Workflow definition sync complete",
        extra={
            "synced": result["synced"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "orphaned": result["orphaned"],
            "total": total,
        },
    )

    return result


def sync_bundled_rules(db: DatabaseProtocol, rules_path: Path | None = None) -> dict[str, Any]:
    """Sync rule YAML files to workflow_definitions table with workflow_type='rule'.

    Rule YAML files use the new format with a top-level `rules:` dict where each
    entry defines a rule with `event` and `effect` fields. File-level fields
    (`group`, `tags`, `sources`) are inherited by all rules in the file.

    Args:
        db: Database connection.
        rules_path: Path to rules directory. Defaults to bundled rules path.

    Returns:
        Dict with success status and counts.
    """
    if rules_path is None:
        rules_path = get_bundled_rules_path()

    # Repair rows where workflow_type was silently changed from 'rule'
    # (e.g. via PUT /api/workflows/{id} before workflow_type was made immutable).
    # Identifies rules by their definition structure: having both event and effect.
    repaired = db.execute(
        "UPDATE workflow_definitions "
        "SET workflow_type = 'rule', updated_at = datetime('now') "
        "WHERE workflow_type != 'rule' "
        "  AND json_extract(definition_json, '$.event') IS NOT NULL "
        "  AND (json_extract(definition_json, '$.effect') IS NOT NULL "
        "       OR json_extract(definition_json, '$.effects') IS NOT NULL)",
    ).rowcount
    if repaired:
        logger.info("Repaired %d rows with incorrect workflow_type (should be 'rule')", repaired)

    result: dict[str, Any] = {
        "success": True,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    if not rules_path.exists():
        logger.debug("Rules path not found", extra={"path": str(rules_path)})
        return result

    manager = LocalWorkflowDefinitionManager(db)

    for yaml_file in sorted(rules_path.rglob("*.yaml")):
        # Skip deprecated directory (old group files moved here)
        if "deprecated" in yaml_file.relative_to(rules_path).parts:
            continue
        try:
            raw_content = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)

            if not isinstance(data, dict):
                logger.warning("Skipping non-dict YAML", extra={"file": str(yaml_file)})
                continue

            # Detect rule YAML format: must have 'rules' dict
            rules_dict = data.get("rules")
            if not isinstance(rules_dict, dict):
                logger.debug("No 'rules' key in YAML, skipping", extra={"file": str(yaml_file)})
                result["skipped"] += 1
                continue

            # File-level defaults
            # Derive group from subdirectory name if not explicitly set
            rel_parts = yaml_file.relative_to(rules_path).parts
            dir_group = rel_parts[0] if len(rel_parts) > 1 else None
            file_group = data.get("group") or dir_group
            file_tags = data.get("tags") or []
            if "gobby" not in file_tags:
                file_tags = [*file_tags, "gobby"]
            file_sources = data.get("sources")

            for rule_name, rule_data in rules_dict.items():
                if not isinstance(rule_data, dict):
                    result["errors"].append(f"Rule '{rule_name}' in {yaml_file.name} is not a dict")
                    continue

                try:
                    _sync_single_rule(
                        manager=manager,
                        rule_name=rule_name,
                        rule_data=rule_data,
                        file_group=file_group,
                        file_tags=file_tags,
                        file_sources=file_sources,
                        result=result,
                    )
                except Exception as e:
                    error_msg = f"Failed to sync rule '{rule_name}' from {yaml_file.name}: {e}"
                    logger.warning(error_msg)
                    result["errors"].append(error_msg)

        except Exception as e:
            error_msg = f"Failed to parse rule file '{yaml_file}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    # Orphan cleanup: soft-delete bundled rules whose YAML was removed.
    # Collects rule names from disk, then marks any DB rows with source='bundled'
    # (or 'template' after rename) that aren't in the set. This handles rules
    # moved to deprecated/ or removed entirely.
    on_disk: set[str] = set()
    for yf in sorted(rules_path.rglob("*.yaml")):
        if "deprecated" in yf.relative_to(rules_path).parts:
            continue
        try:
            d = yaml.safe_load(yf.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("rules"), dict):
                on_disk.update(d["rules"].keys())
        except Exception:
            pass  # Already logged above during main loop

    orphan_rows = db.fetchall(
        "SELECT id, name FROM workflow_definitions "
        "WHERE source = 'template' AND workflow_type = 'rule' "
        "AND deleted_at IS NULL",
    )
    result["orphaned"] = 0
    for row in orphan_rows:
        if row["name"] not in on_disk:
            manager.delete(row["id"])
            logger.info("Soft-deleted orphaned bundled rule", extra={"rule": row["name"]})
            result["orphaned"] += 1

    _ensure_gobby_tag_on_installed(manager, "rule")

    # Propagate tags from templates to installed copies where they've drifted
    # (fixes bug #9657: create_rule doesn't propagate definition_json tags to row-level tags)
    db.execute(
        "UPDATE workflow_definitions SET tags = ("
        "  SELECT t.tags FROM workflow_definitions t"
        "  WHERE t.name = workflow_definitions.name"
        "    AND t.source = 'template' AND t.deleted_at IS NULL"
        ") WHERE source = 'installed' AND deleted_at IS NULL"
        "  AND workflow_type = 'rule'"
        "  AND tags != ("
        "    SELECT t.tags FROM workflow_definitions t"
        "    WHERE t.name = workflow_definitions.name"
        "      AND t.source = 'template' AND t.deleted_at IS NULL"
        "  )",
    )

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.info(
        "Rule definition sync complete",
        extra={
            "synced": result["synced"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "orphaned": result["orphaned"],
            "total": total,
        },
    )

    return result


def _ensure_gobby_tag_on_installed(
    manager: LocalWorkflowDefinitionManager,
    workflow_type: str,
) -> None:
    """Ensure all template/installed rows of a given type have the 'gobby' tag."""
    rows = manager.list_all(workflow_type=workflow_type, include_deleted=False)
    for row in rows:
        if row.source in ("template", "installed"):
            tags = row.tags or []
            if "gobby" not in tags:
                manager.update(row.id, tags=[*tags, "gobby"])


def _propagate_to_installed(
    manager: LocalWorkflowDefinitionManager,
    rule_name: str,
    definition_json: str,
    tags: list[str] | None = None,
) -> None:
    """Propagate definition_json and tags from a template to its installed copy.

    Preserves the installed copy's enabled state.
    """
    installed_row = manager.db.fetchone(
        "SELECT * FROM workflow_definitions "
        "WHERE name = ? AND source = 'installed' AND deleted_at IS NULL",
        (rule_name,),
    )
    if installed_row:
        from gobby.storage.workflow_definitions import WorkflowDefinitionRow

        installed = WorkflowDefinitionRow.from_row(installed_row)
        updates: dict[str, Any] = {}
        if installed.definition_json != definition_json:
            updates["definition_json"] = definition_json
        if tags is not None and set(tags or []) != set(installed.tags or []):
            updates["tags"] = tags
        if updates:
            manager.update(installed.id, **updates)
            logger.info(
                "Propagated changes to installed copy",
                extra={"rule": rule_name, "fields": list(updates.keys())},
            )


def _sync_single_rule(
    manager: LocalWorkflowDefinitionManager,
    rule_name: str,
    rule_data: dict[str, Any],
    file_group: str | None,
    file_tags: list[str] | None,
    file_sources: list[str] | None,
    result: dict[str, Any],
) -> None:
    """Sync a single rule to workflow_definitions.

    Validates against RuleDefinitionBody, then creates or updates the row.
    """
    # Build the RuleDefinitionBody dict
    body_dict: dict[str, Any] = {
        "event": rule_data.get("event"),
    }
    # Support both singular 'effect' and plural 'effects'
    if "effects" in rule_data:
        body_dict["effects"] = rule_data["effects"]
    else:
        body_dict["effect"] = rule_data.get("effect")
    if rule_data.get("when"):
        body_dict["when"] = rule_data["when"]
    if rule_data.get("match"):
        body_dict["match"] = rule_data["match"]
    # Inherit group from file level, rule level overrides
    group = rule_data.get("group", file_group)
    if group:
        body_dict["group"] = group
    if rule_data.get("agent_scope"):
        body_dict["agent_scope"] = rule_data["agent_scope"]
    if rule_data.get("tools"):
        body_dict["tools"] = rule_data["tools"]

    # Validate with Pydantic
    try:
        RuleDefinitionBody(**body_dict)
    except ValidationError as ve:
        raise ValueError(f"Invalid rule definition: {ve}") from ve

    definition_json = json.dumps(body_dict)
    priority = rule_data.get("priority", 100)
    description = rule_data.get("description")
    enabled = rule_data.get("enabled", False)

    # Check if rule already exists
    existing = manager.get_by_name(rule_name, include_deleted=True, include_templates=True)

    if existing is not None:
        # Restore soft-deleted templates so they're always available
        if existing.deleted_at is not None:
            if existing.source == "template":
                manager.restore(existing.id)
                manager.update(
                    existing.id,
                    name=rule_name,
                    definition_json=definition_json,
                    workflow_type="rule",
                    project_id=None,
                    description=description,
                    enabled=False,
                    priority=priority,
                    sources=file_sources,
                    tags=file_tags,
                    source="template",
                )
                logger.info(
                    "Restored soft-deleted bundled rule",
                    extra={"rule": rule_name},
                )
                result["updated"] += 1
            else:
                result["skipped"] += 1
            return

        if existing.source == "template":
            def_changed = existing.definition_json != definition_json
            tags_changed = set(file_tags or []) != set(existing.tags or [])
            if not def_changed and not tags_changed:
                result["skipped"] += 1
            else:
                # Preserve user's enabled toggle on updates
                manager.update(
                    existing.id,
                    name=rule_name,
                    definition_json=definition_json,
                    workflow_type="rule",
                    project_id=None,
                    description=description,
                    enabled=existing.enabled,
                    priority=priority,
                    sources=file_sources,
                    tags=file_tags,
                    source="template",
                )
                # Propagate changes to installed copy (preserve enabled)
                _propagate_to_installed(manager, rule_name, definition_json, tags=file_tags)
                result["updated"] += 1
        else:
            # Non-template copy shadows the template row — get_by_name prefers
            # non-template over template. Look up the template row directly and
            # update it if the definition has changed on disk.
            template_row = manager.db.fetchone(
                "SELECT * FROM workflow_definitions WHERE name = ? AND source = 'template'",
                (rule_name,),
            )
            if template_row:
                from gobby.storage.workflow_definitions import WorkflowDefinitionRow

                template = WorkflowDefinitionRow.from_row(template_row)
                if template.deleted_at:
                    manager.restore(template.id)
                    manager.update(
                        template.id,
                        name=rule_name,
                        definition_json=definition_json,
                        workflow_type="rule",
                        project_id=None,
                        description=description,
                        enabled=False,
                        priority=priority,
                        sources=file_sources,
                        tags=file_tags,
                        source="template",
                    )
                    logger.info(
                        "Restored soft-deleted template behind installed copy",
                        extra={"rule": rule_name},
                    )
                    result["updated"] += 1
                else:
                    def_changed = template.definition_json != definition_json
                    tags_changed = set(file_tags or []) != set(template.tags or [])
                    if def_changed or tags_changed:
                        manager.update(
                            template.id,
                            name=rule_name,
                            definition_json=definition_json,
                            workflow_type="rule",
                            project_id=None,
                            description=description,
                            enabled=template.enabled,
                            priority=priority,
                            sources=file_sources,
                            tags=file_tags,
                            source="template",
                        )
                        # Propagate to the installed copy that shadows this template
                        if existing.source == "installed":
                            _propagate_to_installed(
                                manager, rule_name, definition_json, tags=file_tags
                            )
                        result["updated"] += 1
                    else:
                        result["skipped"] += 1
            else:
                # No template row exists — create one
                manager.create(
                    name=rule_name,
                    definition_json=definition_json,
                    workflow_type="rule",
                    project_id=None,
                    description=description,
                    enabled=False,
                    priority=priority,
                    sources=file_sources,
                    tags=file_tags,
                    source="template",
                )
                logger.info(
                    "Created missing template row behind installed copy",
                    extra={"rule": rule_name},
                )
                result["synced"] += 1
        return

    # Create new rule
    manager.create(
        name=rule_name,
        definition_json=definition_json,
        workflow_type="rule",
        project_id=None,
        description=description,
        enabled=enabled,
        priority=priority,
        sources=file_sources,
        tags=file_tags,
        source="template",
    )
    result["synced"] += 1


def get_bundled_variables_path() -> Path:
    """Get the path to bundled variables directory.

    Returns:
        Path to src/gobby/install/shared/variables/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "variables"


def sync_bundled_variables(db: DatabaseProtocol) -> dict[str, Any]:
    """Sync bundled variable definitions from install/shared/variables/ to the database.

    Variable YAML files use a ``variables:`` dict where each key is the variable
    name and the value contains ``value`` and optional ``description``.  File-level
    ``tags`` are inherited by all variables in the file.

    Args:
        db: Database connection.

    Returns:
        Dict with success status and counts.
    """
    variables_path = get_bundled_variables_path()

    result: dict[str, Any] = {
        "success": True,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    if not variables_path.exists():
        logger.debug("Variables path not found", extra={"path": str(variables_path)})
        return result

    manager = LocalWorkflowDefinitionManager(db)

    for yaml_file in sorted(variables_path.glob("*.yaml")):
        try:
            raw_content = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)

            if not isinstance(data, dict):
                logger.warning("Skipping non-dict YAML", extra={"file": str(yaml_file)})
                continue

            variables_dict = data.get("variables")
            if not isinstance(variables_dict, dict):
                logger.debug("No 'variables' key in YAML, skipping", extra={"file": str(yaml_file)})
                result["skipped"] += 1
                continue

            file_tags = data.get("tags") or []
            if "gobby" not in file_tags:
                file_tags = [*file_tags, "gobby"]

            for var_name, var_data in variables_dict.items():
                if not isinstance(var_data, dict):
                    result["errors"].append(
                        f"Variable '{var_name}' in {yaml_file.name} is not a dict"
                    )
                    continue

                try:
                    _sync_single_variable(
                        manager=manager,
                        var_name=var_name,
                        var_data=var_data,
                        file_tags=file_tags,
                        result=result,
                    )
                except Exception as e:
                    error_msg = f"Failed to sync variable '{var_name}' from {yaml_file.name}: {e}"
                    logger.warning(error_msg)
                    result["errors"].append(error_msg)

        except Exception as e:
            error_msg = f"Failed to parse variable file '{yaml_file}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    # Orphan cleanup: collect all variable names from disk, soft-delete DB rows
    # whose names are no longer present on disk.
    on_disk: set[str] = set()
    for yf in sorted(variables_path.glob("*.yaml")):
        try:
            d = yaml.safe_load(yf.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("variables"), dict):
                on_disk.update(d["variables"].keys())
        except Exception:
            pass

    orphan_rows = db.fetchall(
        "SELECT id, name FROM workflow_definitions "
        "WHERE source = 'template' AND workflow_type = 'variable' AND deleted_at IS NULL",
    )
    result["orphaned"] = 0
    for row in orphan_rows:
        if row["name"] not in on_disk:
            manager.delete(row["id"])
            logger.info("Soft-deleted orphaned bundled variable", extra={"variable": row["name"]})
            result["orphaned"] += 1

    _ensure_gobby_tag_on_installed(manager, "variable")

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.info(
        "Variable definition sync complete",
        extra={
            "synced": result["synced"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "orphaned": result["orphaned"],
            "total": total,
        },
    )

    return result


def _sync_single_variable(
    manager: LocalWorkflowDefinitionManager,
    var_name: str,
    var_data: dict[str, Any],
    file_tags: list[str] | None,
    result: dict[str, Any],
) -> None:
    """Sync a single variable to workflow_definitions.

    Validates against VariableDefinitionBody, then creates or updates the row.
    """
    from gobby.workflows.definitions import VariableDefinitionBody

    body_dict: dict[str, Any] = {
        "variable": var_name,
        "value": var_data.get("value"),
    }
    if var_data.get("description"):
        body_dict["description"] = var_data["description"]

    try:
        VariableDefinitionBody(**body_dict)
    except ValidationError as ve:
        raise ValueError(f"Invalid variable definition: {ve}") from ve

    definition_json = json.dumps(body_dict)
    description = var_data.get("description")

    existing = manager.get_by_name(var_name, include_deleted=True, include_templates=True)

    if existing is not None:
        if existing.deleted_at is not None:
            if existing.source == "template":
                manager.restore(existing.id)
                manager.update(
                    existing.id,
                    name=var_name,
                    definition_json=definition_json,
                    workflow_type="variable",
                    project_id=None,
                    description=description,
                    enabled=True,
                    priority=100,
                    source="template",
                    tags=file_tags,
                )
                logger.info("Restored soft-deleted bundled variable", extra={"variable": var_name})
                result["updated"] += 1
            else:
                result["skipped"] += 1
            return

        if existing.source == "template":
            if existing.definition_json == definition_json:
                result["skipped"] += 1
            else:
                manager.update(
                    existing.id,
                    name=var_name,
                    definition_json=definition_json,
                    workflow_type="variable",
                    project_id=None,
                    description=description,
                    enabled=existing.enabled,
                    priority=100,
                    source="template",
                    tags=file_tags,
                )
                _propagate_to_installed(manager, var_name, definition_json)
                result["updated"] += 1
        else:
            template_row = manager.db.fetchone(
                "SELECT * FROM workflow_definitions WHERE name = ? AND source = 'template'",
                (var_name,),
            )
            if template_row:
                from gobby.storage.workflow_definitions import WorkflowDefinitionRow

                template = WorkflowDefinitionRow.from_row(template_row)
                if template.deleted_at:
                    manager.restore(template.id)
                    manager.update(
                        template.id,
                        name=var_name,
                        definition_json=definition_json,
                        workflow_type="variable",
                        project_id=None,
                        description=description,
                        enabled=True,
                        priority=100,
                        source="template",
                        tags=file_tags,
                    )
                    result["updated"] += 1
                elif template.definition_json != definition_json:
                    manager.update(
                        template.id,
                        name=var_name,
                        definition_json=definition_json,
                        workflow_type="variable",
                        project_id=None,
                        description=description,
                        enabled=template.enabled,
                        priority=100,
                        source="template",
                        tags=file_tags,
                    )
                    if existing.source == "installed":
                        _propagate_to_installed(manager, var_name, definition_json)
                    result["updated"] += 1
                else:
                    result["skipped"] += 1
            else:
                manager.create(
                    name=var_name,
                    definition_json=definition_json,
                    workflow_type="variable",
                    project_id=None,
                    description=description,
                    enabled=True,
                    priority=100,
                    source="template",
                    tags=file_tags,
                )
                result["synced"] += 1
        return

    manager.create(
        name=var_name,
        definition_json=definition_json,
        workflow_type="variable",
        project_id=None,
        description=description,
        enabled=True,
        priority=100,
        source="template",
        tags=file_tags,
    )
    result["synced"] += 1
