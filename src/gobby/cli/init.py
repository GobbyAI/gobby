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
@click.option("--name", help="Project name")
@click.option("--github-url", help="GitHub repository URL")
@click.pass_context
def init(ctx: click.Context, name: str | None, github_url: str | None) -> None:
    """Initialize a new Gobby project in the current directory."""
    cwd = Path.cwd()

    try:
        result = initialize_project(cwd=cwd, name=name, github_url=github_url)
    except Exception as e:
        click.echo(f"Failed to initialize project: {e}", err=True)
        sys.exit(1)

    if result.already_existed:
        click.echo(f"Project already initialized: {result.project_name}")
        click.echo(f"  Project ID: {result.project_id}")
    else:
        click.echo(f"Initialized project '{result.project_name}' in {cwd}")
        click.echo(f"  Project ID: {result.project_id}")
        click.echo(f"  Config: {cwd / '.gobby' / 'project.json'}")
