"""First-run onboarding wizard — delegates to bundled Ink (React CLI) app."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 # subprocess needed to run bundled Node app
import sys

import click

from gobby.paths import get_gobby_home

from .utils import get_install_dir


@click.command()
def setup() -> None:
    """First-run setup wizard. Guides you through installing and configuring Gobby."""
    node = shutil.which("node")
    if not node:
        click.echo(
            "Error: Node.js is required for the setup wizard.\n"
            "Install from https://nodejs.org or: brew install node"
        )
        sys.exit(1)

    # Find bundled setup app
    bundle = get_install_dir() / "shared" / "setup" / "setup.mjs"
    if not bundle.exists():
        click.echo(f"Error: Setup bundle not found at {bundle}")
        click.echo("Run from source: cd web && npm run setup")
        sys.exit(1)

    # Pass context to the Ink app via environment variables
    env = os.environ.copy()
    env["GOBBY_HOME"] = str(get_gobby_home())
    env["GOBBY_INSTALL_DIR"] = str(get_install_dir())
    gobby_bin = shutil.which("gobby")
    if gobby_bin:
        env["GOBBY_BIN"] = gobby_bin
    env["GOBBY_SKIP_BOOTSTRAP"] = "1"  # Skip bootstrap step (we're already installed)

    result = subprocess.run(  # nosec B603
        [node, str(bundle)],
        env=env,
    )
    sys.exit(result.returncode)
