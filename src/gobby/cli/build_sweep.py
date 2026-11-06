"""Report or remove stale agent Cargo build folders."""

from __future__ import annotations

from pathlib import Path

import click

from gobby.agents.build_root import configure_build_root
from gobby.agents.build_sweep import SweepFinding, legacy_empty_cache_target, sweep_build_root
from gobby.cli.runtime import get_cli_runtime
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.json_helpers import json_dumps
from gobby.utils.machine_id import require_machine_id

# The clone listing caps at its limit; this ceiling reads every registered clone.
_ALL_ROWS = 2_147_483_647


@click.command("build-sweep")
@click.option(
    "--apply", is_flag=True, help="Remove stale unowned build folders. Default: report only."
)
@click.option("--json", "as_json", is_flag=True, help="Emit findings as JSON.")
@click.pass_context
def build_sweep(ctx: click.Context, apply: bool, as_json: bool) -> None:
    """Report or remove agent Cargo build folders that no registered checkout owns."""
    runtime = get_cli_runtime(ctx)
    try:
        root = configure_build_root(
            runtime.require_config(apply_migrations=False).agent_build.build_root
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    checkouts = _registered_checkouts(runtime.require_database(apply_migrations=False))
    findings: list[SweepFinding] = sweep_build_root(root, checkouts, apply=apply)
    legacy = legacy_empty_cache_target()
    if legacy is not None:
        findings.append(legacy)
    if as_json:
        payload = [
            {
                "path": str(finding.path),
                "action": finding.action.value,
                "size_bytes": finding.size_bytes,
                "detail": finding.detail,
            }
            for finding in findings
        ]
        click.echo(json_dumps(payload, sort_keys=True))
        return
    for finding in findings:
        detail = f"  {finding.detail}" if finding.detail else ""
        click.echo(f"{finding.action.value:<13} {finding.size_bytes:>12} {finding.path}{detail}")


def _registered_checkouts(database: HubDatabase) -> list[Path]:
    """Return every project checkout, worktree and clone path registered on this machine."""
    machine_id = require_machine_id()
    paths = [
        checkout.root_path
        for checkout in LocalProjectCheckoutManager(database).list_for_machine(machine_id)
    ]
    paths.extend(
        worktree.worktree_path
        for worktree in LocalWorktreeManager(database).list_worktrees(limit=None)
    )
    paths.extend(
        clone.clone_path for clone in LocalCloneManager(database).list_clones(limit=_ALL_ROWS)
    )
    return [Path(path) for path in paths]
