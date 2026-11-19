"""Loader and public API for the hook pipeline corpus.

Each case is one ``HookManager.handle`` call with every collaborator wrapped in a recorder.
The ``expect`` block holds the response and the ordered collaborator calls, recorded from the
Python pipeline rather than written by hand.

``collaborator_script`` lists the healthy results each recorded collaborator returns. An
injected failure lives in one place only: a ``collaborator_failure`` case names the collaborator
in ``failing_collaborator`` and the failure in ``failure`` (``{"method": ..., "error": ...}``);
no script entry carries a failure. Removing those two keys leaves the healthy twin. A call to a
collaborator method the script does not list returns ``None``, so a twin never fails merely
because its script is shorter.

This module is a signature-level stub. Every function returns an empty value and reads
nothing from disk, so the contract tests fail on their assertions until the loader, the
coverage checks and the replay are implemented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).resolve().parent / "pipeline"
SCHEMA_VERSION = 1


def load_corpus(corpus_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(manifest, cases)`` for a pipeline corpus directory.

    Raises ``ValueError`` naming the offender for a stale ``schema_version``, an unknown
    case kind or an unknown collaborator name.
    """
    return {}, []


def check_coverage(corpus_dir: Path, axis: str) -> None:
    """Raise ``ValueError`` naming the first required item on ``axis`` that no case carries."""
    return None


def replay_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the Python ``HookManager.handle`` and return the observed output.

    Each call builds a fresh ``HookManager`` and ``EventHandlers``, so a class attribute patched
    before the call is the one the replay runs.
    """
    return {}
