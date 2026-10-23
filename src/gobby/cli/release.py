"""`gobby release batch`: run one release batch for the caller's live window."""

from __future__ import annotations

import click


@click.group()
def release() -> None:
    """Validate, activate and smoke the release window's pinned candidate."""


@release.command("batch")
@click.option("--token", required=True, help="The live release window token.")
@click.option("--generation", type=int, required=True, help="The window's current generation.")
@click.option("--base-sha", default=None, help="Baseline to diff from when no batch has succeeded.")
def batch(token: str, generation: int, base_sha: str | None) -> None:
    """Run the batch in the foreground; the Release Manager starts it in the background."""
    raise NotImplementedError
