"""Loader, coverage, recorder and checker API for the rule-pass corpus.

The red commit ships signature-only stubs: loaders return empty values and checks
accept everything. The corpus tests import these names and fail on their own
assertions until the green commit implements them.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).resolve().parent / "rules"
BUNDLED_RULES_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "workflows"
    / "rules"
)


def load_corpus(corpus_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(manifest, cases)`` for a corpus directory.

    Validates ``schema_version``, case kinds and seam names against the manifest and
    raises ``ValueError`` naming the offender.
    """
    return {}, []


def validate_case(case: dict[str, Any]) -> None:
    """Raise ``ValueError`` naming any missing ``expect`` section or malformed field."""
    return None


def load_bundled_rules() -> list[dict[str, Any]]:
    """Return one parsed body per bundled rule YAML file, loaded through the engine loader."""
    return []


def load_rule_body(path: Path) -> dict[str, Any]:
    """Parse one rule YAML file and raise ``ValueError`` naming any unknown field."""
    return {}


def check_coverage(corpus_dir: Path, axis: str) -> None:
    """Raise ``ValueError`` naming the first required item on ``axis`` that no case carries.

    A response field counts as set when the case's ``expect.response`` carries the key with
    a value that differs from that field's ``HookResponse`` default. A missing key is not set.
    """
    return None


def replay_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the Python rule engine and return the observed ``expect`` block."""
    return {}


def record_case(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one case with every seam wrapped and return the ordered seam call log."""
    return []


def record_corpus(corpus_dir: Path) -> None:
    """Fill each case's ``expect.effect_log`` with the ``record_case`` output.

    Runs only when the ``GOBBY_CORPUS_RECORD`` environment variable is 1; otherwise the
    corpus files are left unchanged, so a default run replays. Each case is written back
    into the case file it was read from, with the same document layout.
    """
    return None


def replay_normalizer_vector(vector: dict[str, Any]) -> Any:
    """Call the normalizer entry point named by a vector and return its observed output."""
    return None


def seam_registry() -> dict[str, tuple[object, str]]:
    """Map each seam kind to the owner (class or module) and attribute the recorder wraps."""
    return {}


def find_bundled_name_mentions(paths: Iterable[Path], names: Iterable[str]) -> list[str]:
    """Return one finding per file that names a bundled definition as a whole token."""
    return []
