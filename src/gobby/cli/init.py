"""
Project initialization commands.
"""

import logging
import sys
from pathlib import Path

import click

from gobby.utils.project_init import initialize_project

logger = logging.getLogger(__name__)


@click.command()
@click.option("--name", "-n", help="Project name")
@click.option("--github-url", "-g", help="GitHub repository URL")
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
    ctx: click.Context, name: str | None, github_url: str | None, working_dir: Path | None
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
                import subprocess

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

        # Check Cisco skill-scanner (skill safety scanning)
        try:
            import skill_scanner  # noqa: F401
        except ImportError:
            click.echo(
                "  Warning: cisco-ai-skill-scanner not found. "
                "Install: uv add cisco-ai-skill-scanner"
            )

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
