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
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from gobby.storage.workflow_audit import WorkflowAuditManager

import click

from gobby.storage.hub.runtime import open_runtime_hub_database
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager


def _get_manager() -> LocalWorkflowDefinitionManager:
    """Get workflow definition manager."""
    db = open_runtime_hub_database(apply_migrations=False)
    return LocalWorkflowDefinitionManager(db)


def _get_audit_manager() -> WorkflowAuditManager:
    """Get workflow audit manager."""
    from gobby.storage.workflow_audit import WorkflowAuditManager

    return WorkflowAuditManager(open_runtime_hub_database(apply_migrations=False))


def _parse_rule_body(row: Any) -> dict[str, Any]:
    """Parse rule definition JSON body."""
    return cast(dict[str, Any], json.loads(row.definition_json))


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
    manager = _get_manager()

    if event:
        rows = manager.list_rules_by_event(event, enabled=enabled_flag)
    elif group:
        rows = manager.list_rules_by_group(group, enabled=enabled_flag)
    else:
        rows = manager.list_all(workflow_type="rule", enabled=enabled_flag)

    if json_output:
        summaries = [_rule_summary(r) for r in rows]
        click.echo(json.dumps({"rules": summaries, "count": len(summaries)}, indent=2))
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
    manager = _get_manager()
    row = manager.get_by_name(name)

    if row is None or row.workflow_type != "rule":
        click.echo(f"Rule not found: {name}", err=True)
        sys.exit(1)

    detail = _rule_detail(row)

    if json_output:
        click.echo(json.dumps(detail, indent=2))
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
        click.echo(f"Match: {json.dumps(detail['match'], indent=2)}")
    if detail.get("effects"):
        click.echo(f"Effects: {json.dumps(detail['effects'], indent=2)}")


@rules.command("enable")
@click.argument("name")
def enable_rule(name: str) -> None:
    """Enable a rule."""
    manager = _get_manager()
    row = manager.get_by_name(name)

    if row is None or row.workflow_type != "rule":
        click.echo(f"Rule not found: {name}", err=True)
        sys.exit(1)

    manager.update(row.id, enabled=True)
    click.echo(f"Enabled rule: {name}")


@rules.command("disable")
@click.argument("name")
def disable_rule(name: str) -> None:
    """Disable a rule."""
    manager = _get_manager()
    row = manager.get_by_name(name)

    if row is None or row.workflow_type != "rule":
        click.echo(f"Rule not found: {name}", err=True)
        sys.exit(1)

    manager.update(row.id, enabled=False)
    click.echo(f"Disabled rule: {name}")


@rules.command("import")
@click.argument("file", type=click.Path())
def import_rules(file: str) -> None:
    """Import rules from a YAML file.

    FILE is a path to a rule YAML file with the standard format:
    group, rules dict with event/effect fields.
    """
    path = Path(file)

    if not path.exists():
        click.echo(f"File not found: {file}", err=True)
        sys.exit(1)

    if path.suffix.lower() not in {".yaml", ".yml"}:
        click.echo("Rule file must have .yaml or .yml extension.", err=True)
        sys.exit(1)

    from gobby.workflows.sync_rules import sync_bundled_rules

    db = open_runtime_hub_database(apply_migrations=False)
    result = sync_bundled_rules(db, rules_path=path.parent)

    if result.get("errors"):
        for err in result["errors"]:
            click.echo(f"Error: {err}", err=True)
        sys.exit(1)

    synced = result.get("synced", 0)
    updated = result.get("updated", 0)
    click.echo(f"Imported rules: {synced} new, {updated} updated")


@rules.command("export")
@click.option("--group", "-g", default=None, help="Export only rules in this group")
def export_rules(group: str | None) -> None:
    """Export rules as YAML."""
    import yaml

    manager = _get_manager()

    if group:
        rows = manager.list_rules_by_group(group, enabled=None)
    else:
        rows = manager.list_all(workflow_type="rule")

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
    audit = _get_audit_manager()
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
        click.echo(json.dumps(output, indent=2))
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
