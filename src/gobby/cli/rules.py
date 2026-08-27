"""Rules CLI commands.

Provides CLI commands for managing standalone rules:
- list: List rules with filters
- show: Show rule details
- enable: Enable a rule
- disable: Disable a rule
- import: Import rules from a YAML file
- export: Export rules as YAML
- audit: Show rule evaluation audit log
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from gobby.utils.json_helpers import json_dumps

if TYPE_CHECKING:
    from gobby.storage.workflow_audit import WorkflowAuditManager

import click

from gobby.cli.runtime import require_cli_database
from gobby.cli.utils_config import get_daemon_client
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.utils.daemon_client import DaemonClient


def _get_manager() -> RuleDefinitionManager:
    """Get typed rule definition manager."""
    db = require_cli_database()
    return RuleDefinitionManager(db)


@contextmanager
def _manager_context() -> Iterator[RuleDefinitionManager]:
    """Yield a workflow definition manager borrowing the CLI runtime database."""
    manager = _get_manager()
    yield manager


def _get_audit_manager() -> WorkflowAuditManager:
    """Get workflow audit manager."""
    from gobby.storage.workflow_audit import WorkflowAuditManager

    return WorkflowAuditManager(require_cli_database())


@contextmanager
def _audit_manager_context() -> Iterator[WorkflowAuditManager]:
    """Yield a workflow audit manager borrowing the CLI runtime database."""
    manager = _get_audit_manager()
    yield manager


def _get_daemon_client(ctx: click.Context) -> DaemonClient:
    """Get daemon client from CLI context."""
    return get_daemon_client()


def _response_detail(response: Any) -> str:
    """Extract a useful daemon error detail."""
    try:
        payload = response.json()
    except Exception:
        return str(getattr(response, "text", ""))
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error")
        if detail:
            return str(detail)
    return str(getattr(response, "text", ""))


def _toggle_rule_via_daemon(ctx: click.Context, name: str, *, enabled: bool) -> None:
    """Toggle a rule through the daemon so live rule state is updated in-process."""
    client = _get_daemon_client(ctx)
    endpoint = f"/api/rules/{quote(name, safe='')}/toggle"
    try:
        response = client.call_http_api(endpoint, method="PUT", json_data={"enabled": enabled})
    except Exception as e:
        click.echo(f"Error toggling rule via daemon: {e}", err=True)
        sys.exit(1)

    if response.status_code == 200:
        return

    if response.status_code == 404:
        click.echo(f"Rule not found: {name}", err=True)
        sys.exit(1)

    detail = _response_detail(response)
    message = f"Error toggling rule: HTTP {response.status_code}"
    if detail:
        message = f"{message}: {detail}"
    click.echo(message, err=True)
    sys.exit(1)


def _parse_rule_body(row: Any) -> dict[str, Any]:
    """Parse rule definition JSON body."""
    payload = row.definition_json
    if isinstance(payload, dict):
        return payload
    return cast(dict[str, Any], json.loads(payload))


def _rule_summary(row: Any) -> dict[str, Any]:
    """Build summary dict for display."""
    body = _parse_rule_body(row)
    return {
        "name": row.name,
        "event": body.get("event"),
        "group": body.get("group"),
        "enabled": row.enabled,
        "priority": row.priority,
        "source": row.source,
        "description": row.description,
    }


def _rule_detail(row: Any) -> dict[str, Any]:
    """Build full detail dict."""
    body = _parse_rule_body(row)
    return {
        "name": row.name,
        "event": body.get("event"),
        "group": body.get("group"),
        "when": body.get("when"),
        "match": body.get("match"),
        "effects": body.get("effects") or ([body["effect"]] if body.get("effect") else None),
        "enabled": row.enabled,
        "priority": row.priority,
        "source": row.source,
        "description": row.description,
        "tags": row.tags,
    }


@click.group()
def rules() -> None:
    """Manage Gobby rules."""
    pass


@rules.command("list")
@click.option("--event", "-e", default=None, help="Filter by event type")
@click.option("--group", "-g", default=None, help="Filter by group")
@click.option(
    "--enabled", "enabled_flag", flag_value=True, default=None, help="Show only enabled rules"
)
@click.option("--disabled", "enabled_flag", flag_value=False, help="Show only disabled rules")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def list_rules(
    event: str | None,
    group: str | None,
    enabled_flag: bool | None,
    json_output: bool,
) -> None:
    """List rules with optional filters."""
    with _manager_context() as manager:
        if event:
            rows = manager.list_by_event(event, enabled=enabled_flag)
        elif group:
            rows = manager.list_by_group(group, enabled=enabled_flag)
        else:
            rows = manager.list_all(enabled=enabled_flag)

    if json_output:
        summaries = [_rule_summary(r) for r in rows]
        click.echo(json_dumps({"rules": summaries, "count": len(summaries)}, indent=2))
        return

    if not rows:
        click.echo("No rules found.")
        return

    for row in rows:
        body = _parse_rule_body(row)
        status = "on " if row.enabled else "off"
        event_str = body.get("event", "?")
        group_str = body.get("group", "")
        group_tag = f" [{group_str}]" if group_str else ""
        desc = f" - {row.description}" if row.description else ""
        click.echo(f"  {status}  {row.name}{group_tag}  ({event_str}){desc}")


@rules.command("show")
@click.argument("name")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def show_rule(name: str, json_output: bool) -> None:
    """Show details of a specific rule."""
    with _manager_context() as manager:
        row = manager.get_by_name(name)

    if row is None:
        click.echo(f"Rule not found: {name}", err=True)
        sys.exit(1)

    detail = _rule_detail(row)

    if json_output:
        click.echo(json_dumps(detail, indent=2))
        return

    click.echo(f"Name: {detail['name']}")
    if detail.get("description"):
        click.echo(f"Description: {detail['description']}")
    click.echo(f"Event: {detail.get('event', '?')}")
    if detail.get("group"):
        click.echo(f"Group: {detail['group']}")
    click.echo(f"Enabled: {detail['enabled']}")
    click.echo(f"Priority: {detail['priority']}")
    click.echo(f"Source: {detail['source']}")
    if detail.get("when"):
        click.echo(f"When: {detail['when']}")
    if detail.get("tags"):
        click.echo(f"Tags: {', '.join(detail['tags'])}")
    if detail.get("match"):
        click.echo(f"Match: {json_dumps(detail['match'], indent=2)}")
    if detail.get("effects"):
        click.echo(f"Effects: {json_dumps(detail['effects'], indent=2)}")


@rules.command("enable")
@click.argument("name")
@click.pass_context
def enable_rule(ctx: click.Context, name: str) -> None:
    """Enable a rule."""
    _toggle_rule_via_daemon(ctx, name, enabled=True)
    click.echo(f"Enabled rule: {name}")


@rules.command("disable")
@click.argument("name")
@click.pass_context
def disable_rule(ctx: click.Context, name: str) -> None:
    """Disable a rule."""
    _toggle_rule_via_daemon(ctx, name, enabled=False)
    click.echo(f"Disabled rule: {name}")


@rules.command("import")
@click.argument("file", type=click.Path())
def import_rules(file: str) -> None:
    """Import rules from a YAML file.

    FILE is a path to a rule YAML file with the standard format:
    group, rules dict with event/effect fields. Run inside a registered
    project, the rules belong to that project (the same scope ``gobby
    install`` gives ``.gobby/workflows/rules/``); elsewhere they are global.
    """
    path = Path(file)

    if not path.exists():
        click.echo(f"File not found: {file}", err=True)
        sys.exit(1)

    if path.suffix.lower() not in {".yaml", ".yml"}:
        click.echo("Rule file must have .yaml or .yml extension.", err=True)
        sys.exit(1)

    from gobby.cli.installers.shared import registered_project_id
    from gobby.workflows.sync_rules import sync_rule_file

    db = require_cli_database()
    project_id = registered_project_id(db, Path.cwd())
    result = sync_rule_file(db, rule_file=path, project_id=project_id)

    if result.get("errors"):
        for err in result["errors"]:
            click.echo(f"Error: {err}", err=True)
        sys.exit(1)

    synced = result.get("synced", 0)
    updated = result.get("updated", 0)
    scope = f"project {project_id}" if project_id else "global"
    click.echo(f"Imported rules: {synced} new, {updated} updated ({scope})")


@rules.command("export")
@click.option("--group", "-g", default=None, help="Export only rules in this group")
def export_rules(group: str | None) -> None:
    """Export rules as YAML."""
    import yaml

    with _manager_context() as manager:
        if group:
            rows = manager.list_by_group(group, enabled=None)
        else:
            rows = manager.list_all()

    if not rows:
        click.echo("No rules to export.")
        return

    # Group rules by group field
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        body = _parse_rule_body(row)
        rule_group = body.get("group", "ungrouped")
        if rule_group not in groups:
            groups[rule_group] = {}
        # Build rule entry
        rule_entry: dict[str, Any] = {}
        if row.description:
            rule_entry["description"] = row.description
        rule_entry["event"] = body.get("event")
        if body.get("when"):
            rule_entry["when"] = body["when"]
        if body.get("match"):
            rule_entry["match"] = body["match"]
        if body.get("effects"):
            rule_entry["effects"] = body["effects"]
        elif body.get("effect"):
            rule_entry["effects"] = [body["effect"]]
        groups[rule_group][row.name] = rule_entry

    # Output each group as a YAML document
    for grp_name, grp_rules in sorted(groups.items()):
        doc = {"group": grp_name, "rules": grp_rules}
        click.echo(yaml.dump(doc, default_flow_style=False, sort_keys=False))


@rules.command("audit")
@click.option("--session", "-s", "session_id", default=None, help="Filter by session ID")
@click.option("--limit", "-n", default=50, help="Maximum entries to show")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def audit_rules(session_id: str | None, limit: int, json_output: bool) -> None:
    """Show rule evaluation audit log."""
    with _audit_manager_context() as audit:
        entries = audit.get_entries(session_id=session_id, limit=limit)

    if json_output:
        output = []
        for entry in entries:
            output.append(
                {
                    "id": entry.id,
                    "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
                    "event_type": entry.event_type,
                    "tool_name": getattr(entry, "tool_name", None),
                    "rule_id": getattr(entry, "rule_id", None),
                    "result": entry.result,
                    "reason": getattr(entry, "reason", None),
                }
            )
        click.echo(json_dumps(output, indent=2))
        return

    if not entries:
        click.echo("No audit entries found.")
        return

    for entry in entries:
        ts = entry.timestamp.strftime("%H:%M:%S") if entry.timestamp else "?"
        result_str = entry.result.upper() if entry.result else "?"
        tool = getattr(entry, "tool_name", "?")
        rule = getattr(entry, "rule_id", "")
        reason = getattr(entry, "reason", "")
        rule_tag = f" [{rule}]" if rule else ""
        reason_tag = f" - {reason}" if reason else ""
        click.echo(f"  {ts}  {result_str:6s}  {entry.event_type}  {tool}{rule_tag}{reason_tag}")
