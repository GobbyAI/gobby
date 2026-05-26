"""Rule definition synchronization from bundled YAML templates.

Single-row model: templates live on disk only. The DB holds installed rows
directly. Bundled ``gobby`` rows are refreshed in place when the on-disk YAML
changes, while user/custom rows remain protected. Soft-deleted rows are not
restored.
"""

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import json_array_contains_condition, json_text_expr
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.utils.native_bin import resolve_native_bin
from gobby.workflows.definitions import RuleDefinitionBody

logger = logging.getLogger(__name__)


def get_bundled_rules_path() -> Path:
    """Get the path to bundled rules directory.

    Returns:
        Path to src/gobby/install/shared/workflows/rules/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "workflows" / "rules"


def get_bundled_rules_paths() -> list[Path]:
    """Get active bundled rule roots."""
    from gobby.paths import get_install_dir

    install_dir = get_install_dir()
    return [
        install_dir / "shared" / "workflows" / "rules",
        install_dir / "shared" / "rules",
    ]


def _iter_active_rule_files(rules_paths: list[Path]) -> list[tuple[Path, Path]]:
    """Return non-deprecated YAML files from active rule roots."""
    files: list[tuple[Path, Path]] = []
    for root in rules_paths:
        for yaml_file in sorted(root.rglob("*.yaml")):
            if "deprecated" in yaml_file.relative_to(root).parts:
                continue
            files.append((root, yaml_file))
    return files


def sync_bundled_rules(
    db: HubDatabase,
    rules_path: Path | None = None,
    tag: str = "gobby",
) -> dict[str, Any]:
    """Sync rule YAML files to workflow_definitions table with workflow_type='rule'.

    Creates installed rows directly from template files. Existing bundled
    ``gobby`` rows are refreshed when the YAML changes, preserving the
    user's enabled toggle. User/custom rows are not overwritten.

    Args:
        db: Database connection.
        rules_path: Path to rules directory. Defaults to bundled rules path.
        tag: Tag to apply to synced rules. Defaults to "gobby" for bundled.

    Returns:
        Dict with success status and counts.
    """
    rules_paths = get_bundled_rules_paths() if rules_path is None else [rules_path]

    # Repair rows where workflow_type was silently changed from 'rule'
    event_expr = json_text_expr(db, "definition_json", "event")
    effect_expr = json_text_expr(db, "definition_json", "effect")
    effects_expr = json_text_expr(db, "definition_json", "effects")
    repaired = db.execute(
        "UPDATE workflow_definitions "
        "SET workflow_type = 'rule', updated_at = CURRENT_TIMESTAMP "
        "WHERE workflow_type != 'rule' "
        f"  AND {event_expr} IS NOT NULL "
        f"  AND ({effect_expr} IS NOT NULL "
        f"       OR {effects_expr} IS NOT NULL)",
    ).rowcount
    if repaired:
        logger.info(f"Repaired {repaired} rows with incorrect workflow_type (should be 'rule')")

    result: dict[str, Any] = {
        "success": True,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    existing_paths = [path for path in rules_paths if path.exists()]
    if not existing_paths:
        logger.debug("Rules path not found", extra={"paths": [str(path) for path in rules_paths]})
        return result

    manager = LocalWorkflowDefinitionManager(db)
    on_disk: set[str] = set()

    for current_rules_path, yaml_file in _iter_active_rule_files(existing_paths):
        if "deprecated" in yaml_file.relative_to(current_rules_path).parts:
            continue
        try:
            raw_content = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)

            if not isinstance(data, dict):
                logger.warning("Skipping non-dict YAML", extra={"file": str(yaml_file)})
                continue

            rules_dict = data.get("rules")
            if not isinstance(rules_dict, dict):
                logger.debug("No 'rules' key in YAML, skipping", extra={"file": str(yaml_file)})
                result["skipped"] += 1
                continue

            # File-level defaults
            rel_parts = yaml_file.relative_to(current_rules_path).parts
            dir_group = rel_parts[0] if len(rel_parts) > 1 else None
            file_group = data.get("group") or dir_group
            file_tags = data.get("tags") or []
            if tag not in file_tags:
                file_tags = [*file_tags, tag]
            file_sources = data.get("sources")
            file_audience = data.get("audience")

            for rule_name, rule_data in rules_dict.items():
                if not isinstance(rule_data, dict):
                    result["errors"].append(f"Rule '{rule_name}' in {yaml_file.name} is not a dict")
                    continue

                on_disk.add(rule_name)

                # Name collision prevention: user templates can't shadow gobby rules
                if tag != "gobby":
                    gobby_tag_condition, gobby_tag_params = json_array_contains_condition(
                        db, "tags", "gobby"
                    )
                    gobby_row = db.fetchone(
                        "SELECT id FROM workflow_definitions "
                        f"WHERE name = ? AND {gobby_tag_condition} "
                        "AND deleted_at IS NULL",
                        (rule_name, *gobby_tag_params),
                    )
                    if gobby_row:
                        logger.debug(
                            "Skipping user rule that collides with gobby rule",
                            extra={"rule": rule_name},
                        )
                        result["skipped"] += 1
                        continue

                try:
                    _sync_single_rule(
                        manager=manager,
                        rule_name=rule_name,
                        rule_data=rule_data,
                        file_group=file_group,
                        file_tags=file_tags,
                        file_sources=file_sources,
                        file_audience=file_audience,
                        sync_tag=tag,
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

    # Orphan cleanup: soft-delete rules whose YAML was removed.
    # Only touch rows with matching tag to avoid cross-tag damage.
    tag_condition, tag_params = json_array_contains_condition(db, "tags", tag)
    orphan_rows = db.fetchall(
        "SELECT id, name FROM workflow_definitions "
        "WHERE workflow_type = 'rule' "
        f"AND {tag_condition} AND deleted_at IS NULL",
        tag_params,
    )
    result["orphaned"] = 0
    for row in orphan_rows:
        if row["name"] not in on_disk:
            manager.delete(row["id"])
            logger.info("Soft-deleted orphaned rule", extra={"rule": row["name"], "tag": tag})
            result["orphaned"] += 1

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.info(
        "Rule definition sync complete",
        extra={
            "synced": result["synced"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "orphaned": result.get("orphaned", 0),
            "total": total,
        },
    )

    return result


def resolve_sync_placeholders(definition_json: str) -> str:
    """Replace sync-time placeholders in a rule definition.

    Currently supports:
    - ``{{ gobby_bin }}``: resolved to the absolute path of the ``gobby``
      binary via ``shutil.which``, falling back to
      ``<sys.executable> -m gobby`` when the binary isn't on PATH.
    - ``{{ gsqz_bin }}``: resolved to the absolute path of the ``gsqz``
      binary. Checks ``~/.gobby/bin/gsqz`` first, then ``shutil.which``.

    Called once per rule during sync so the DB always stores a concrete path
    that works regardless of whether the binaries are on the CLI's PATH.
    """
    if "{{ gobby_bin }}" in definition_json:
        gobby_bin = shutil.which("gobby")
        if not gobby_bin:
            gobby_bin = f"{sys.executable} -m gobby"
        definition_json = definition_json.replace("{{ gobby_bin }}", gobby_bin)

    if "{{ gsqz_bin }}" in definition_json:
        gsqz_bin = _resolve_gsqz_bin()
        definition_json = definition_json.replace("{{ gsqz_bin }}", gsqz_bin)

    return definition_json


def _resolve_gsqz_bin() -> str:
    """Resolve the gsqz binary path."""
    return resolve_native_bin("gsqz") or "gsqz"


def _sync_single_rule(
    manager: LocalWorkflowDefinitionManager,
    rule_name: str,
    rule_data: dict[str, Any],
    file_group: str | None,
    file_tags: list[str] | None,
    file_sources: list[str] | None,
    file_audience: str | None,
    sync_tag: str,
    result: dict[str, Any],
) -> None:
    """Sync a single rule to workflow_definitions.

    Creates an installed row if none exists. Existing bundled ``gobby`` rows
    are refreshed when the YAML changes, preserving the user's enabled toggle.
    Soft-deleted rows and user/custom rows are not restored or overwritten.
    """
    # Build the RuleDefinitionBody dict
    body_dict: dict[str, Any] = {
        "event": rule_data.get("event"),
    }
    if "effects" in rule_data:
        body_dict["effects"] = rule_data["effects"]
    elif "effect" in rule_data:
        body_dict["effects"] = [rule_data["effect"]]
    if rule_data.get("when"):
        body_dict["when"] = rule_data["when"]
    if rule_data.get("match"):
        body_dict["match"] = rule_data["match"]
    group = rule_data.get("group", file_group)
    if group:
        body_dict["group"] = group
    if rule_data.get("agent_scope"):
        body_dict["agent_scope"] = rule_data["agent_scope"]
    audience = rule_data.get("audience", file_audience)
    if audience:
        body_dict["audience"] = audience
    if rule_data.get("tools"):
        body_dict["tools"] = rule_data["tools"]

    try:
        RuleDefinitionBody(**body_dict)
    except ValidationError as ve:
        raise ValueError(f"Invalid rule definition: {ve}") from ve

    definition_json = resolve_sync_placeholders(json.dumps(body_dict))
    priority = rule_data.get("priority", 100)
    description = rule_data.get("description")
    enabled = rule_data.get("enabled", False)

    # Check if rule already exists (any source, including soft-deleted)
    existing = manager.get_by_name(rule_name, include_deleted=True)

    if existing is not None:
        # Respect soft-deletes — don't re-create rules the user removed
        if existing.deleted_at is not None:
            result["skipped"] += 1
            return

        if _is_sync_managed_bundled_rule(existing, sync_tag):
            update_fields = _build_rule_update_fields(
                existing=existing,
                definition_json=definition_json,
                description=description,
                priority=priority,
                sources=file_sources,
                tags=file_tags,
            )
            if update_fields:
                manager.update(existing.id, **update_fields)
                result["updated"] += 1
                return

        result["skipped"] += 1
        return

    # Create new installed rule directly
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
        source="installed",
    )
    result["synced"] += 1


def _is_sync_managed_bundled_rule(existing: Any, sync_tag: str) -> bool:
    """Return whether an existing row is safe for bundled sync to overwrite."""
    if sync_tag != "gobby":
        return False
    return (
        existing.source == "installed"
        and existing.project_id is None
        and sync_tag in (existing.tags or [])
    )


def _build_rule_update_fields(
    *,
    existing: Any,
    definition_json: str,
    description: str | None,
    priority: int,
    sources: list[str] | None,
    tags: list[str] | None,
) -> dict[str, Any]:
    """Build the minimal field set needed to refresh a bundled rule row."""
    update_fields: dict[str, Any] = {}

    if not _json_payloads_equal(existing.definition_json, definition_json):
        update_fields["definition_json"] = definition_json
    if existing.description != description:
        update_fields["description"] = description
    if existing.priority != priority:
        update_fields["priority"] = priority
    if existing.sources != sources:
        update_fields["sources"] = sources
    if existing.tags != tags:
        update_fields["tags"] = tags

    return update_fields


def _json_payloads_equal(left: str, right: str) -> bool:
    try:
        left_payload: object = json.loads(left)
        right_payload: object = json.loads(right)
        return left_payload == right_payload
    except (TypeError, ValueError, json.JSONDecodeError):
        return left == right
