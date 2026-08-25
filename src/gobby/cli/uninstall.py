"""Uninstall Gobby CLI integrations and managed tools."""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap
from gobby.ui_exposure import UiExposeError, disable_tailscale_ui

from ._install_prompts import _echo_uninstall_summary, _run_standard_cli_uninstall
from .install_setup_impeccable import remove_impeccable_runtime
from .install_setup_rtk import disable_rule_if_present, remove_managed_rtk
from .installers import (
    uninstall_agy,
    uninstall_claude,
    uninstall_codex,
    uninstall_droid,
    uninstall_grok,
    uninstall_qwen,
)
from .runtime import CliRuntime, get_cli_runtime


def _teardown_ui_exposure() -> None:
    """Best-effort removal of the managed Tailscale UI exposure."""
    try:
        bootstrap = load_bootstrap(resolve_database_url=False)
    except BootstrapConfigError as exc:
        click.echo(f"Warning: could not read UI exposure intent: {exc}", err=True)
        return
    if bootstrap.ui_expose is None:
        return
    try:
        disable_tailscale_ui(bootstrap.daemon_port)
    except UiExposeError as exc:
        click.echo(
            f"Warning: could not remove Tailscale UI exposure: {exc}. "
            "Run 'gobby ui unexpose' to retry.",
            err=True,
        )
        return
    click.echo("Removed Tailscale UI exposure.")


@click.command("uninstall")
@click.option(
    "--claude",
    "claude_flag",
    is_flag=True,
    help="Uninstall Claude Code hooks only",
)
@click.option(
    "--grok",
    "grok_flag",
    is_flag=True,
    help="Uninstall Grok CLI hooks only",
)
@click.option(
    "--codex",
    "codex_flag",
    is_flag=True,
    help="Uninstall Codex notify integration",
)
@click.option(
    "--droid",
    "droid_flag",
    is_flag=True,
    help="Uninstall Droid CLI hooks only",
)
@click.option(
    "--agy",
    "agy_flag",
    is_flag=True,
    help="Uninstall AGY CLI hooks only",
)
@click.option(
    "--qwen",
    "qwen_flag",
    is_flag=True,
    help="Uninstall Qwen CLI hooks only",
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Uninstall hooks from all CLIs (default behavior when no flags specified)",
)
@click.option(
    "--tools",
    "tools_flag",
    is_flag=True,
    help="Remove Gobby-managed tools and their owned materialization caches",
)
@click.option(
    "--project",
    "project_flag",
    is_flag=True,
    help="Uninstall per-project hooks from current directory (instead of global)",
)
@click.option(
    "-C",
    "--path",
    "working_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Target directory (default: current directory)",
)
@click.confirmation_option(prompt="Are you sure you want to uninstall Gobby hooks?")
def uninstall(
    claude_flag: bool,
    grok_flag: bool,
    agy_flag: bool,
    codex_flag: bool,
    droid_flag: bool,
    qwen_flag: bool,
    all_flag: bool,
    tools_flag: bool,
    project_flag: bool,
    working_dir: Path | None,
) -> None:
    """Uninstall Gobby hooks and selected managed tools.

    By default (no flags), uninstalls global hooks from CLI settings and ~/.gobby/hooks/.
    Use --project to uninstall per-project hooks from the current directory.
    Use --claude, --grok, --agy, --qwen, or --codex to uninstall only from
    specific CLIs.
    """
    if tools_flag and project_flag:
        raise click.UsageError("--tools cannot be combined with --project")
    project_path = working_dir.resolve() if working_dir else Path.cwd()

    if (
        not claude_flag
        and not grok_flag
        and not agy_flag
        and not qwen_flag
        and not codex_flag
        and not droid_flag
        and not all_flag
        and not tools_flag
    ):
        all_flag = True

    if not project_flag:
        try:
            runtime = get_cli_runtime()
        except RuntimeError:
            runtime = CliRuntime(config_file=None)
        try:
            disable_rule_if_present(runtime.require_database())
        except (BootstrapConfigError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            click.echo(f"Warning: could not disable RTK rewrite rule: {exc}", err=True)
        finally:
            runtime.close()

    if tools_flag:
        cleanup = remove_impeccable_runtime()
        for path in cleanup.removed:
            click.echo(f"Removed managed artifact: {path}")
        for warning in cleanup.skipped:
            click.echo(f"Warning: {warning}", err=True)
        rtk_cleanup = remove_managed_rtk()
        for path in rtk_cleanup.removed:
            click.echo(f"Removed managed artifact: {path}")
        for warning in rtk_cleanup.conflicts:
            click.echo(f"Warning: {warning}", err=True)
        if not any((claude_flag, grok_flag, agy_flag, qwen_flag, codex_flag, droid_flag, all_flag)):
            return

    clis_to_uninstall: list[str] = []

    if all_flag:
        if project_flag:
            claude_settings = project_path / ".claude" / "settings.json"
            grok_hooks = Path.home() / ".grok" / "hooks" / "gobby.json"
            agy_hooks = Path.home() / ".gemini" / "config" / "hooks.json"
            qwen_settings = project_path / ".qwen" / "settings.json"
            codex_hooks = project_path / ".codex" / "hooks.json"
            droid_hooks = project_path / ".factory" / "hooks" / "hooks.json"
        else:
            claude_settings = Path.home() / ".claude" / "settings.json"
            grok_hooks = Path.home() / ".grok" / "hooks" / "gobby.json"
            agy_hooks = Path.home() / ".gemini" / "config" / "hooks.json"
            qwen_settings = Path.home() / ".qwen" / "settings.json"
            codex_hooks = Path.home() / ".codex" / "hooks.json"
            droid_hooks = Path.home() / ".factory" / "hooks" / "hooks.json"

        if claude_settings.exists():
            clis_to_uninstall.append("claude")
        if grok_hooks.exists():
            clis_to_uninstall.append("grok")
        if agy_hooks.exists():
            clis_to_uninstall.append("agy")
        if qwen_settings.exists():
            clis_to_uninstall.append("qwen")
        if codex_hooks.exists():
            clis_to_uninstall.append("codex")
        if droid_hooks.exists():
            clis_to_uninstall.append("droid")

        if not clis_to_uninstall:
            click.echo("No Gobby hooks found to uninstall.")
            if project_flag:
                click.echo(f"\nChecked: {project_path / '.claude'}")
                click.echo(f"         {Path.home() / '.grok' / 'hooks' / 'gobby.json'}")
                click.echo(f"         {Path.home() / '.gemini' / 'config' / 'hooks.json'}")
                click.echo(f"         {project_path / '.qwen'}")
                click.echo(f"         {project_path / '.codex'}")
                click.echo(f"         {project_path / '.factory'}")
            else:
                click.echo(f"\nChecked: {Path.home() / '.claude'}")
                click.echo(f"         {Path.home() / '.grok' / 'hooks' / 'gobby.json'}")
                click.echo(f"         {Path.home() / '.gemini' / 'config' / 'hooks.json'}")
                click.echo(f"         {Path.home() / '.qwen'}")
                click.echo(f"         {Path.home() / '.codex'}")
                click.echo(f"         {Path.home() / '.factory'}")
            sys.exit(0)
    else:
        if claude_flag:
            clis_to_uninstall.append("claude")
        if grok_flag:
            clis_to_uninstall.append("grok")
        if agy_flag:
            clis_to_uninstall.append("agy")
        if qwen_flag:
            clis_to_uninstall.append("qwen")
        if codex_flag:
            clis_to_uninstall.append("codex")
        if droid_flag:
            clis_to_uninstall.append("droid")

    click.echo("=" * 60)
    click.echo("  Gobby Hooks Uninstallation")
    click.echo("=" * 60)
    if project_flag:
        click.echo(f"\nScope: Project ({project_path})")
    else:
        click.echo("\nScope: Global")
    click.echo(f"Targets to uninstall: {', '.join(clis_to_uninstall)}")
    click.echo("")

    uninstall_base = project_path if project_flag else Path.home()
    results: dict[str, dict[str, Any]] = {}
    standard_uninstallers: dict[str, Callable[..., dict[str, Any]]] = {
        "agy": uninstall_agy,
        "claude": uninstall_claude,
        "grok": uninstall_grok,
        "qwen": uninstall_qwen,
        "codex": uninstall_codex,
        "droid": uninstall_droid,
    }
    for cli_name, uninstaller_fn in standard_uninstallers.items():
        if cli_name in clis_to_uninstall:
            uninstall_kwargs: dict[str, Any] = {}
            if cli_name in {"qwen", "droid"}:
                uninstall_kwargs["mode"] = "project" if project_flag else "global"
            _run_standard_cli_uninstall(
                cli_name,
                uninstaller_fn,
                uninstall_base,
                results,
                **uninstall_kwargs,
            )

    if not project_flag and all_flag:
        global_hooks_dir = Path(
            os.environ.get("GOBBY_HOOKS_DIR", str(Path.home() / ".gobby" / "hooks"))
        )
        for fname in ("hook_dispatcher.py", "validate_settings.py"):
            fpath = global_hooks_dir / fname
            if fpath.exists():
                try:
                    fpath.unlink()
                except OSError as exc:
                    click.echo(f"  Warning: could not remove {fpath}: {exc}", err=True)
        click.echo("Removed global hook dispatchers from ~/.gobby/hooks/")
        click.echo("")
        _teardown_ui_exposure()

    all_success = _echo_uninstall_summary(results)
    if not all_success:
        sys.exit(1)
