"""Sync MCP server instance YAML into mcp_servers rows."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from gobby.mcp_proxy.templates import MCPServerTemplate, expand_template
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore

logger = logging.getLogger(__name__)


def sync_mcp_server_files(
    db: HubDatabase,
    roots: list[Path],
    *,
    project_id: str | None,
    project_root: Path | None,
    secret_store: SecretStore,
) -> dict[str, Any]:
    """Read instance YAML and upsert ``mcp_servers`` rows. Never deletes rows."""
    result: dict[str, Any] = {
        "synced": 0,
        "updated": 0,
        "affected_ids": [],
        "needs_configuration": {},
        "optional_missing": {},
        "errors": [],
    }
    manager = LocalMCPManager(db)
    for root in roots:
        if not root.exists():
            continue
        try:
            files = _iter_yaml_files(root)
        except OSError as exc:
            result["errors"].append(f"Failed to read instance root '{root}': {exc}")
            continue
        scope = GLOBAL_PROJECT_ID
        if project_root is not None and root == project_root and project_id:
            scope = project_id
        for yaml_file in files:
            _sync_instance_file(
                manager,
                yaml_file,
                scope=scope,
                secret_store=secret_store,
                result=result,
            )
    return result


def _iter_yaml_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for yaml_file in sorted(root.rglob("*.yaml")):
        if "deprecated" in yaml_file.relative_to(root).parts:
            continue
        files.append(yaml_file)
    return files


def _sync_instance_file(
    manager: LocalMCPManager,
    yaml_file: Path,
    *,
    scope: str,
    secret_store: SecretStore,
    result: dict[str, Any],
) -> None:
    try:
        raw = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    except Exception as exc:
        result["errors"].append(f"Failed to parse instance '{yaml_file}': {exc}")
        return
    if not isinstance(raw, dict):
        result["errors"].append(f"Instance '{yaml_file}' must be a mapping")
        return

    template_name = raw.get("template")
    if not isinstance(template_name, str) or not template_name:
        result["errors"].append(f"Instance '{yaml_file}' is missing template")
        return
    instance_name = raw.get("name")
    if instance_name is None:
        instance_name = yaml_file.stem
    if not isinstance(instance_name, str) or not instance_name:
        result["errors"].append(f"Instance '{yaml_file}' has an invalid name")
        return
    instance_name = instance_name.lower()

    template_row = manager.get_template(template_name, project_id=scope)
    if template_row is None:
        result["errors"].append(f"Unknown template '{template_name}'")
        return
    if not template_row.enabled:
        result["errors"].append(
            f"template_disabled: template '{template_name}' is disabled in scope "
            f"{template_row.project_id}"
        )
        return

    values_raw = raw.get("values") or {}
    if not isinstance(values_raw, dict):
        result["errors"].append(f"Instance '{yaml_file}' values must be a mapping")
        return
    values = {str(key): str(value) for key, value in values_raw.items()}
    try:
        template = MCPServerTemplate.from_definition(template_row.definition)
        expanded = expand_template(
            template,
            name=instance_name,
            project_id=scope,
            values=values,
            description=raw.get("description") if isinstance(raw.get("description"), str) else None,
            secret_exists=lambda n: secret_store.exists(n, project_id=scope),
        )
    except Exception as exc:
        result["errors"].append(
            f"Failed to expand instance '{instance_name}' from template '{template_name}': {exc}"
        )
        return

    existing = manager.get_server(instance_name, project_id=scope)
    config = expanded.config
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True
    server = manager.upsert(
        name=instance_name,
        transport=config.transport,
        url=config.url,
        command=config.command,
        args=config.args,
        env=config.env,
        headers=config.headers,
        enabled=enabled,
        description=config.description,
        connect_timeout=config.connect_timeout,
        template_id=template_row.id,
        template_values=expanded.template_values,
        runtime_hook=config.runtime_hook,
        project_id=scope,
    )
    result["affected_ids"].append(server.id)
    if existing is None:
        result["synced"] += 1
    else:
        result["updated"] += 1
    if expanded.missing_secrets:
        result["needs_configuration"][instance_name] = list(expanded.missing_secrets)
        global_flag = " --global" if scope == GLOBAL_PROJECT_ID else ""
        for secret_name in expanded.missing_secrets:
            logger.info("gobby secrets set %s%s", secret_name, global_flag)
    if expanded.optional_missing_secrets:
        result["optional_missing"][instance_name] = list(expanded.optional_missing_secrets)
