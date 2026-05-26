"""
Project initialization commands.
"""

import asyncio
import importlib.util
import logging
import subprocess
import sys
from pathlib import Path

import click

from gobby.utils.native_bin import resolve_native_bin
from gobby.utils.project_init import initialize_project

logger = logging.getLogger(__name__)


@click.command()
@click.option("--name", "-n", help="Project name")
@click.option("--github-url", "-g", help="GitHub repository URL")
@click.option(
    "--linear-setup/--no-linear-setup",
    default=None,
    help="Run guided Linear setup after project initialization",
)
@click.option("--linear-team-id", help="Linear team ID for --linear-setup")
@click.option("--linear-project-id", help="Existing Linear project ID for --linear-setup")
@click.option(
    "-C",
    "--path",
    "working_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Target directory (default: current directory)",
)
@click.pass_context
def init(
    ctx: click.Context,
    name: str | None,
    github_url: str | None,
    linear_setup: bool | None,
    linear_team_id: str | None,
    linear_project_id: str | None,
    working_dir: Path | None,
) -> None:
    """Initialize a new Gobby project in the current directory."""
    cwd = working_dir.resolve() if working_dir else Path.cwd()

    try:
        result = initialize_project(cwd=cwd, name=name, github_url=github_url)
    except Exception as e:
        click.echo(f"Failed to initialize project: {e}", err=True)
        sys.exit(1)

    if result.already_existed:
        click.echo(f"Project already initialized: {result.project_name}")
        click.echo(f"  Project ID: {result.project_id}")
    else:
        # Hint for first-time users
        setup_state = Path("~/.gobby/setup_state.json").expanduser()
        if not setup_state.exists():
            click.echo("Tip: For first-time setup, try `gobby setup` for a guided experience.")
            click.echo()
        click.echo(f"Initialized project '{result.project_name}' in {cwd}")
        click.echo(f"  Project ID: {result.project_id}")
        click.echo(f"  Config: {cwd / '.gobby' / 'project.json'}")

        # Trigger initial code indexing via gcode
        try:
            gcode_bin = resolve_native_bin("gcode")
            if gcode_bin:
                click.echo("Indexing codebase...")
                proc = subprocess.run(
                    [gcode_bin, "index", "--project", str(result.project_path)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if proc.returncode == 0:
                    if proc.stdout:
                        click.echo(proc.stdout.rstrip())
                else:
                    detail = proc.stderr.strip() if proc.stderr else "(no details)"
                    click.echo(f"Code indexing failed: {detail}", err=True)
            else:
                click.echo(
                    "gcode not installed — skipping initial index. Run `gobby install`.", err=True
                )
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
            click.echo(f"Code indexing skipped: {e}", err=True)

        # Check tmux availability
        import shutil

        from gobby.agents.tmux.wsl_compat import needs_wsl

        if needs_wsl():
            if not shutil.which("wsl"):
                click.echo(
                    "  Warning: WSL not found. Install: wsl --install, then: sudo apt install tmux"
                )
            else:
                # WSL available — check if tmux is installed inside it
                try:
                    tmux_check = subprocess.run(
                        ["wsl", "which", "tmux"],
                        capture_output=True,
                        timeout=5,
                    )
                    if tmux_check.returncode != 0:
                        click.echo(
                            "  Warning: tmux not found inside WSL. "
                            "Install: wsl -e sudo apt install tmux"
                        )
                except (subprocess.TimeoutExpired, OSError):
                    pass  # WSL may be slow to start; don't block init
        elif not shutil.which("tmux"):
            import platform as _platform

            if _platform.system() == "Darwin":
                click.echo("  Warning: tmux not found. Install: brew install tmux")
            else:
                click.echo(
                    "  Warning: tmux not found. Install: sudo apt install tmux "
                    "(or sudo dnf install tmux)"
                )

        # Check clawhub CLI (skill hub search)
        if not shutil.which("clawhub"):
            click.echo("  Warning: clawhub CLI not found. Install: npm i -g clawhub")

        # Check ClawCare (skill safety scanning)
        if importlib.util.find_spec("clawcare") is None:
            click.echo("  Warning: clawcare not found. Install: uv add clawcare")

        # Show detected verification commands
        if result.verification:
            verification_dict = result.verification.to_dict()
            if verification_dict:
                click.echo("  Detected verification commands:")
                for key, value in verification_dict.items():
                    if key != "custom":
                        if value is None:
                            continue
                        click.echo(f"    {key}: {value}")
                    elif value:  # custom dict
                        if isinstance(value, dict):
                            for custom_name, custom_cmd in value.items():
                                click.echo(f"    {custom_name}: {custom_cmd}")
                        else:
                            click.echo(f"    custom: {value}")

    _maybe_run_linear_setup(
        result.project_id,
        linear_setup=linear_setup,
        team_id=linear_team_id,
        linear_project_id=linear_project_id,
    )


def _maybe_run_linear_setup(
    project_id: str,
    linear_setup: bool | None,
    team_id: str | None,
    linear_project_id: str | None,
) -> None:
    should_setup = linear_setup is True
    if linear_setup is None and sys.stdin.isatty():
        should_setup = click.confirm("Set up Linear sync for this project now?", default=False)
    if not should_setup:
        return

    try:
        from gobby.cli.linear import _create_linear_mcp_manager, _run_linear_setup
        from gobby.storage.hub.runtime import open_runtime_hub_database
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.tasks import LocalTaskManager

        db = open_runtime_hub_database(apply_migrations=False)
        project_manager = LocalProjectManager(db)
        result = asyncio.run(
            _run_linear_setup(
                task_manager=LocalTaskManager(db),
                mcp_manager=_create_linear_mcp_manager(db, project_id),
                project_manager=project_manager,
                project_id=project_id,
                bootstrap=True,
                team_id=team_id,
                linear_project_id=linear_project_id,
                project_name=None,
                import_issues=False,
                create_missing=False,
            )
        )
    except Exception as e:
        click.echo(f"Linear setup failed: {e}", err=True)
        sys.exit(1)

    click.echo("Linear setup complete")
    click.echo(f"  Team: {result['linear_team_id']}")
    click.echo(f"  Project: {result['linear_project_name']} ({result['linear_project_id']})")
