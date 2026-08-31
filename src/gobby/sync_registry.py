"""Canonical bundled-content sync fan-out.

``reload_cache``, daemon dev-mode startup, and the bundled-reinstall path call
:func:`sync_bundled_content_to_db` directly. ``gobby install`` and ``gobby
sync`` go through ``gobby.cli.installers.shared.sync_bundled_content_to_db``,
which wraps this fan-out and adds user-content import (rules, variables, MCP
templates, and instance YAML) outside dev mode.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

# (content_type, module_path, function_name)
SYNC_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("skills", "gobby.skills.sync", "sync_bundled_skills"),
    ("prompts", "gobby.prompts.sync", "sync_bundled_prompts"),
    ("agents", "gobby.agents.sync", "sync_bundled_agents"),
    ("pipelines", "gobby.workflows.sync_pipelines", "sync_bundled_pipelines"),
    ("rules", "gobby.workflows.sync_rules", "sync_bundled_rules"),
    ("variables", "gobby.workflows.sync_variables", "sync_bundled_variables"),
    ("build_profiles", "gobby.storage.build_profiles", "sync_bundled_build_profiles"),
    (
        "detection_manifests",
        "gobby.agents.detection.registry",
        "sync_bundled_detection_manifests",
    ),
    (
        "mcp_templates",
        "gobby.mcp_proxy.sync_templates",
        "sync_bundled_mcp_templates",
    ),
)


def migrate_rule_delivery_dispositions(db: HubDatabase) -> dict[str, Any]:
    """Narrow rule-disposition migration entry point used at daemon startup."""
    from gobby.workflows.sync_rules import (
        migrate_rule_delivery_dispositions as _migrate_rule_delivery_dispositions,
    )

    return _migrate_rule_delivery_dispositions(db)


def sync_bundled_content_to_db(
    db: HubDatabase,
    *,
    only: Iterable[str] | None = None,
    skip_types: set[str] | None = None,
) -> dict[str, Any]:
    """Sync bundled content definitions into the database.

    Args:
        db: Database connection implementing HubDatabase.
        only: If set, sync only these content types.
        skip_types: Content type names to skip (e.g. tampered types).

    Returns:
        Dict with total_synced count, per-type details, and any errors.
    """
    result: dict[str, Any] = {
        "total_synced": 0,
        "errors": [],
        "details": {},
    }
    bundled_changes: dict[str, int] = {}
    allowed = set(only) if only is not None else None

    for content_type, module_path, func_name in SYNC_TARGETS:
        if allowed is not None and content_type not in allowed:
            continue
        if skip_types and content_type in skip_types:
            logger.debug("Skipping sync of bundled %s", content_type)
            result["details"][content_type] = {"skipped": True}
            continue
        try:
            module = __import__(module_path, fromlist=[func_name])
            sync_fn = getattr(module, func_name)
            sync_result = sync_fn(db)
            synced = sync_result.get("synced", 0) + sync_result.get("updated", 0)
            result["total_synced"] += synced
            result["details"][content_type] = sync_result
            changed = sum(
                value
                for key in ("synced", "updated", "orphaned", "purged_project_overrides")
                if isinstance((value := sync_result.get(key)), int)
            )
            if changed > 0:
                bundled_changes[content_type] = changed
                logger.debug("Synced %s bundled %s changes to database", changed, content_type)
        except Exception as e:
            msg = f"Failed to sync bundled {content_type}: {e}"
            logger.warning(msg)
            result["errors"].append(msg)
            result["details"][content_type] = {"error": str(e)}

    if bundled_changes:
        logger.info(
            "Bundled content sync changed database state",
            extra={
                "changed": sum(bundled_changes.values()),
                "content_types": bundled_changes,
            },
        )
    else:
        logger.debug(
            "Bundled content sync made no database changes",
            extra={"content_types": list(result["details"])},
        )
    return result
