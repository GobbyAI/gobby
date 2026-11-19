"""Loader and public API for the adapter corpus.

Each case is one adapter call: an inbound payload and the hook event it becomes, an outbound
``HookResponse`` and the provider output it becomes, a node-origin payload whose file does not
exist, a git-source verdict, or a route refusal made through the ASGI path. The ``expect``
blocks are recorded from the Python adapters (``adapters_recorder``), never written by hand.
Coverage checks live in ``adapters_coverage``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).resolve().parent / "adapters"
SCHEMA_VERSION = 1
# A route refusal is posted as a raw request body, so it names no adapter source.
SOURCELESS_KINDS = frozenset({"route_refusal"})


def _validate_case(case: dict[str, Any], kinds: set[str], sources: set[str]) -> None:
    case_id = str(case.get("id", "<no id>"))
    kind = case.get("kind")
    if kind not in kinds:
        raise ValueError(f"{case_id}: unknown case kind {kind!r}")
    if kind in SOURCELESS_KINDS:
        if "request" not in case:
            raise ValueError(f"{case_id}: a route refusal carries the request it posts")
    elif case.get("source") not in sources:
        raise ValueError(f"{case_id}: unknown source {case.get('source')!r}")
    if case.get("provenance") != "synthetic":
        raise ValueError(f"{case_id}: provenance must be 'synthetic'")


def load_corpus(corpus_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(manifest, cases)`` for an adapter corpus directory.

    Raises ``ValueError`` naming the offender for a stale ``schema_version``, an unknown
    source or an unknown case kind.
    """
    manifest_path = corpus_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    version = manifest.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{manifest_path.name}: schema_version {version!r} is not {SCHEMA_VERSION}"
        )
    kinds = {str(kind) for kind in manifest.get("case_kinds", [])}
    sources = {str(source) for source in manifest.get("sources", [])}
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted((corpus_dir / "cases").glob("*.json")):
        for case in json.loads(path.read_text())["cases"]:
            _validate_case(case, kinds, sources)
            if case["id"] in seen:
                raise ValueError(f"duplicate case id {case['id']!r}")
            seen.add(case["id"])
            cases.append(case)
    return manifest, cases


def check_coverage(corpus_dir: Path, axis: str) -> None:
    """Raise ``ValueError`` naming each required item on ``axis`` that no case carries."""
    from tests.contracts import adapters_coverage

    adapters_coverage.check_coverage(corpus_dir, axis)


def replay_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the Python adapter (or the ASGI route) and return the observed output.

    Each call builds a fresh adapter (or app), so a class or module attribute patched before the
    call is the one the replay runs. A ``node_origin`` case is replayed inside a node delivery
    context, so ``node_origin()`` is true for it unless the binding is patched; its payload names
    a file that does not exist, so the hub path would reach that file's read.
    """
    from tests.contracts import adapters_recorder

    return adapters_recorder.replay_case(case)


def record_corpus(corpus_dir: Path) -> None:
    """Fill each case's ``expect`` block when ``GOBBY_CORPUS_RECORD`` is 1; otherwise do nothing."""
    from tests.contracts import adapters_recorder

    adapters_recorder.record_corpus(corpus_dir, write_document)


def write_document(path: Path, document: dict[str, Any]) -> None:
    """Write one case file in the committed layout."""
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
