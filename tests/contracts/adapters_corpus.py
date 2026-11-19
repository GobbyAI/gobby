"""Loader and public API for the adapter corpus.

Each case is one adapter call: an inbound payload and its expected hook event, an outbound
``HookResponse`` and its expected provider output, a node-origin payload without a file, a
git-source verdict, or a route refusal made through the ASGI path. The ``expect`` blocks are
recorded from the Python adapters, never written by hand.

This module is a signature-level stub. Every function returns an empty value and reads
nothing from disk, so the contract tests fail on their assertions until the loader, the
coverage checks and the replay are implemented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).resolve().parent / "adapters"
SCHEMA_VERSION = 1


def load_corpus(corpus_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(manifest, cases)`` for an adapter corpus directory.

    Raises ``ValueError`` naming the offender for a stale ``schema_version``, an unknown
    source, an unknown case kind or an unknown collaborator.
    """
    return {}, []


def check_coverage(corpus_dir: Path, axis: str) -> None:
    """Raise ``ValueError`` naming the first required item on ``axis`` that no case carries."""
    return None


def replay_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the Python adapter (or the ASGI route) and return the observed output.

    Each call builds a fresh adapter (or app), so a class or module attribute patched before the
    call is the one the replay runs.
    """
    return {}
