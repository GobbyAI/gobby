"""Sync MCP server templates from YAML roots into mcp_server_templates."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.templates import (
    MCPServerTemplate,
    expand_template,
    get_bundled_templates_path,
    load_template_file,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.mcp_models import MCPServer
from gobby.storage.mcp_templates import MCPServerTemplateRow
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secret_names import normalize_secret_name
from gobby.storage.secrets import SecretStore
from gobby.workflows.pipeline_loader import detect_override_conflict

logger = logging.getLogger(__name__)

__all__ = ["get_bundled_templates_path", "sync_bundled_mcp_templates"]

_ADOPTION_FIELDS = (
    "transport",
    "url",
    "command",
    "args",
    "env",
    "headers",
    "connect_timeout",
)
_SECRET_REF_PREFIX = "$secret:"


def sync_bundled_mcp_templates(
    db: HubDatabase,
    templates_path: Path | list[Path] | None = None,
    tag: str = "gobby",
    *,
    project_id: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Sync template YAML files into ``mcp_server_templates``.

    Bundled and global user roots write ``GLOBAL_PROJECT_ID``; files under
    ``project_root`` write ``project_id``. ``enabled`` is never passed to
    upsert, so first creation takes the definition toggle and drift keeps the
    stored one. Owner is ``gobby`` when ``tag`` is ``gobby``, otherwise ``user``.
    """
    owner = "gobby" if tag == "gobby" else "user"
    if templates_path is None:
        roots = [get_bundled_templates_path()]
    elif isinstance(templates_path, Path):
        roots = [templates_path]
    else:
        roots = list(templates_path)

    result = _new_result()
    manager = LocalMCPManager(db)
    on_disk: set[tuple[str, str]] = set()
    scanned_scopes: set[str] = set()

    for root in roots:
        if not root.exists():
            continue
        try:
            files = _iter_yaml_files(root)
        except OSError as exc:
            result["errors"].append(f"Failed to read template root '{root}': {exc}")
            continue
        scope = _scope_for_root(root, project_id=project_id, project_root=project_root)
        scanned_scopes.add(scope)
        for yaml_file in files:
            _sync_template_file(
                manager,
                yaml_file,
                owner=owner,
                scope=scope,
                result=result,
                on_disk=on_disk,
            )

    if not result["errors"] and scanned_scopes:
        _prune_missing_templates(manager, owner, scanned_scopes, on_disk, result)

    _adopt_legacy_rows(manager, result)
    return result


def _new_result() -> dict[str, Any]:
    return {
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "orphaned": 0,
        "orphaned_global": 0,
        "errors": [],
        "adoption_skipped": {},
    }


def _scope_for_root(
    root: Path,
    *,
    project_id: str | None,
    project_root: Path | None,
) -> str:
    if project_root is not None and root == project_root:
        return project_id or GLOBAL_PROJECT_ID
    if project_root is None and project_id:
        return project_id
    return GLOBAL_PROJECT_ID


def _iter_yaml_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for yaml_file in sorted(root.rglob("*.yaml")):
        if "deprecated" in yaml_file.relative_to(root).parts:
            continue
        files.append(yaml_file)
    return files


def _exact_template(
    manager: LocalMCPManager, name: str, project_id: str
) -> MCPServerTemplateRow | None:
    row = manager.get_template(name, project_id=project_id)
    if row is None or row.project_id != project_id:
        return None
    return row


def _sync_template_file(
    manager: LocalMCPManager,
    yaml_file: Path,
    *,
    owner: str,
    scope: str,
    result: dict[str, Any],
    on_disk: set[tuple[str, str]],
) -> None:
    try:
        template = load_template_file(yaml_file)
    except Exception as exc:
        result["errors"].append(f"Failed to parse template '{yaml_file}': {exc}")
        return

    on_disk.add((template.name, scope))
    exact = _exact_template(manager, template.name, scope)
    if owner == "gobby" and exact is not None and exact.owner != "gobby":
        logger.warning(
            "Skipping bundled template %s; a user-owned row occupies the same scope",
            template.name,
        )
        result["skipped"] += 1
        return
    if owner == "user":
        bundled = manager.get_template(template.name, project_id=scope)
        if bundled is not None and bundled.owner == "gobby":
            try:
                detect_override_conflict(template, bundled)
            except ValueError as exc:
                result["errors"].append(str(exc))
                return

    try:
        upserted = manager.upsert_template(
            name=template.name,
            project_id=scope,
            owner=owner,
            definition=template.to_definition(),
            source_path=str(yaml_file),
        )
    except Exception as exc:
        result["errors"].append(f"Failed to sync template '{template.name}': {exc}")
        return

    if exact is None:
        result["synced"] += 1
        return
    if upserted.definition_hash != exact.definition_hash or upserted.owner != exact.owner:
        result["updated"] += 1
        return
    result["skipped"] += 1


def _prune_missing_templates(
    manager: LocalMCPManager,
    owner: str,
    scopes: set[str],
    on_disk: set[tuple[str, str]],
    result: dict[str, Any],
) -> None:
    placeholders = ", ".join(["%s"] * len(scopes))
    rows = manager.db.fetchall(
        f"""
        SELECT * FROM mcp_server_templates
        WHERE owner = %s AND project_id IN ({placeholders})
        """,  # nosec B608
        (owner, *scopes),
    )
    for raw in rows:
        row = MCPServerTemplateRow.from_row(raw)
        if (row.name, row.project_id) in on_disk:
            continue
        for instance in manager.list_template_instances(row.id):
            logger.info(
                "Detached MCP instance %s (%s) from pruned template %s",
                instance.name,
                instance.project_id,
                row.name,
            )
        manager.delete_template(row.name, project_id=row.project_id)
        result["orphaned"] += 1
        if row.project_id == GLOBAL_PROJECT_ID:
            result["orphaned_global"] += 1


def _adopt_legacy_rows(manager: LocalMCPManager, result: dict[str, Any]) -> None:
    gobby_rows = [
        MCPServerTemplateRow.from_row(raw)
        for raw in manager.db.fetchall(
            "SELECT * FROM mcp_server_templates WHERE owner = %s AND project_id = %s",
            ("gobby", GLOBAL_PROJECT_ID),
        )
    ]
    by_name = {row.name: row for row in gobby_rows}
    secret_store = SecretStore(manager.db)
    for server in manager.list_servers(GLOBAL_PROJECT_ID, enabled_only=False):
        if server.template_id is not None:
            continue
        template_row = by_name.get(server.name)
        if template_row is None:
            continue
        reason = _try_adopt(manager, template_row, server, secret_store)
        if reason is not None:
            result["adoption_skipped"][server.name] = reason


def _try_adopt(
    manager: LocalMCPManager,
    template_row: MCPServerTemplateRow,
    server: MCPServer,
    secret_store: SecretStore,
) -> str | None:
    template = MCPServerTemplate.from_definition(template_row.definition)
    values = _invert_values(template, server)
    secret_reason = _default_secret_mismatch(template, values)
    if secret_reason is not None:
        return secret_reason
    try:
        expanded = expand_template(
            template,
            name=server.name,
            project_id=str(server.project_id),
            values=values,
            description=server.description,
            secret_exists=lambda n: secret_store.exists(n, project_id=str(server.project_id)),
        )
    except ValueError:
        return "expand"
    diff = _first_runtime_diff(expanded.config, server)
    if diff is not None:
        return diff
    manager.update_server(
        server.name,
        project_id=server.project_id,
        template_id=template_row.id,
        template_values=expanded.template_values,
    )
    return None


def _invert_values(template: MCPServerTemplate, server: MCPServer) -> dict[str, str]:
    values: dict[str, str] = {}
    env = server.env or {}
    args = list(server.args or [])
    for param in template.params:
        if param.env and param.env in env:
            values[param.name] = env[param.env]
            continue
        if param.arg_flag and param.arg_flag in args:
            index = args.index(param.arg_flag)
            if index + 1 < len(args):
                values[param.name] = args[index + 1]
    return values


def _default_secret_mismatch(template: MCPServerTemplate, values: dict[str, str]) -> str | None:
    for param in template.params:
        if not param.secret or not param.default_secret or param.name not in values:
            continue
        actual = _secret_name(values[param.name])
        expected = normalize_secret_name(param.default_secret)
        if actual != expected:
            return "env" if param.env else "args"
    return None


def _secret_name(value: str) -> str:
    if value.startswith(_SECRET_REF_PREFIX):
        return normalize_secret_name(value[len(_SECRET_REF_PREFIX) :])
    return normalize_secret_name(value)


def _first_runtime_diff(expanded: Any, server: MCPServer) -> str | None:
    for field in _ADOPTION_FIELDS:
        left = getattr(expanded, field)
        right = getattr(server, field)
        if field in {"env", "headers"}:
            if (left or {}) != (right or {}):
                return field
        elif field == "args":
            if list(left or []) != list(right or []):
                return field
        elif field == "connect_timeout":
            if float(left) != float(right):
                return field
        elif left != right:
            return field
    return None
