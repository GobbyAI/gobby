"""Uninstall Gobby CLI integrations and managed tools."""

import os
import sys
from pathlib import Path

import click

from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap
from gobby.ui_exposure import UiExposeError, disable_tailscale_ui

from ._install_prompts import _echo_uninstall_summary
from .install_components import UNINSTALLABLE_COMPONENTS, run_uninstall_components
from .runtime import CliRuntime, get_cli_runtime

# Global CLI config files whose presence marks an installed CLI integration.
_GLOBAL_CLI_CONFIGS: tuple[tuple[str, str], ...] = (
    ("claude", ".claude/settings.json"),
    ("grok", ".grok/hooks/gobby.json"),
    ("agy", ".gemini/config/hooks.json"),
    ("qwen", ".qwen/settings.json"),
    ("codex", ".codex/hooks.json"),
    ("droid", ".factory/hooks/hooks.json"),
)


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


def _detected_clis(home: Path) -> list[str]:
    return [name for name, config in _GLOBAL_CLI_CONFIGS if (home / config).exists()]


def _remove_global_dispatchers() -> None:
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


def _cli_runtime() -> CliRuntime:
    try:
        return get_cli_runtime()
    except RuntimeError:
        return CliRuntime(config_file=None)


@click.command("uninstall")
@click.argument(
    "components",
    nargs=-1,
    type=click.Choice(UNINSTALLABLE_COMPONENTS),
    metavar="[COMPONENT]...",
)
@click.option(
    "-C",
    "--path",
    "working_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Repository for the git-hooks component (default: current directory)",
)
@click.confirmation_option(prompt="Are you sure you want to uninstall Gobby hooks?")
def uninstall(components: tuple[str, ...], working_dir: Path | None) -> None:
    """Uninstall Gobby hooks and managed tools.

    Bare `gobby uninstall` removes everything installed: hooks from every
    detected CLI, the global hook dispatchers, the Tailscale UI exposure, the
    RTK rewrite rule and managed binary, and the Impeccable runtime. Docker
    containers, data volumes, bootstrap.yaml, secrets, and the files home are
    never touched. `gobby uninstall COMPONENT...` removes only those
    components; git-hooks targets the repository given with -C.

    Components: claude, codex, grok, qwen, droid, agy, git-hooks, rtk,
    impeccable.
    """
    project_path = working_dir.resolve() if working_dir else Path.cwd()
    full_uninstall = not components
    if full_uninstall:
        home = Path.home()
        detected = _detected_clis(home)
        if not detected:
            click.echo("No Gobby hooks found to uninstall.")
            checked = [str(home / config) for _name, config in _GLOBAL_CLI_CONFIGS]
            click.echo(f"\nChecked: {checked[0]}")
            for path in checked[1:]:
                click.echo(f"         {path}")
        targets: tuple[str, ...] = (*detected, "rtk", "impeccable")
        click.echo("=" * 60)
        click.echo("  Gobby Uninstallation")
        click.echo("=" * 60)
        click.echo(f"\nTargets to uninstall: {', '.join(targets)}")
        click.echo("")
    else:
        targets = tuple(dict.fromkeys(components))

    runtime = _cli_runtime()
    try:
        results = run_uninstall_components(targets, project_path=project_path, runtime=runtime)
    finally:
        runtime.close()

    if full_uninstall:
        _remove_global_dispatchers()
        _teardown_ui_exposure()

    if not _echo_uninstall_summary(results):
        sys.exit(1)
