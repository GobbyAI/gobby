"""Loader and public API for the hook pipeline corpus.

Each case is one delivery through the route's adapter step into ``HookManager.handle``, with
every collaborator wrapped in a recorder (``pipeline_recorder``). The ``expect`` block holds the
response, the adapter's native output and the ordered collaborator reads, recorded from the
Python pipeline rather than written by hand. Coverage checks live in ``pipeline_coverage``.

``collaborator_script`` lists results a collaborator answers in place of its real object. An
injected failure lives in one place only: a ``collaborator_failure`` case names the collaborator
in ``failing_collaborator`` and the failure in ``failure`` (``{"method": ..., "error": ...}``);
no script entry carries a failure. Removing those two keys leaves the healthy twin. A read the
script does not list goes to the real collaborator, or answers ``None`` for a stand-in that has
no real object in the harness, so a twin never fails merely because its script is shorter.

Every delivery, the case's own and each ``setup`` entry, starts from a non-empty native provider
payload with a ``source`` the route's adapter table holds and a ``hook_type`` that adapter's
``EVENT_MAP`` maps. Only an event type no routed adapter produces is delivered directly, as
``event`` with a ``reason``; a delivery never carries both.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).resolve().parent / "pipeline"
SCHEMA_VERSION = 1
SYNTHETIC_RULE_PREFIX = "synthetic-"
# A case delivers either a native provider payload or, for an event type no adapter
# produces, the hook event itself.
DELIVERY_KEYS = ("payload", "event")


def setup_delivery(case: Mapping[str, Any], setup: Mapping[str, Any]) -> dict[str, Any]:
    """Return the delivery one ``setup`` entry makes: the entry over the case's other keys.

    The case's own ``payload`` or ``event`` is not inherited, so a setup entry delivers exactly
    the payload or event it names.
    """
    inherited = {key: value for key, value in case.items() if key not in DELIVERY_KEYS}
    return {**inherited, **setup}


def _validate_delivery(case_id: str, delivery: Mapping[str, Any], where: str) -> None:
    """Require a native provider payload the delivery's source adapter maps, or a direct event."""
    if all(key in delivery for key in DELIVERY_KEYS):
        raise ValueError(f"{case_id}: {where} carries both a payload and an event")
    if "event" in delivery:
        return
    payload = delivery.get("payload")
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError(f"{case_id}: {where} needs a non-empty native provider payload")
    if "source" not in delivery:
        raise ValueError(f"{case_id}: {where} names no source for its payload")
    from tests.contracts import pipeline_recorder

    source = str(delivery["source"])
    adapter = pipeline_recorder.ADAPTERS.get(source)
    if adapter is None:
        raise ValueError(f"{case_id}: {where} names source {source!r}, which no adapter routes")
    hook_type = delivery.get("hook_type")
    if hook_type not in (getattr(adapter, "EVENT_MAP", None) or {}):
        raise ValueError(
            f"{case_id}: {where} names hook type {hook_type!r}, which the {source} adapter "
            "does not map"
        )


def _validate_case(case: dict[str, Any], kinds: set[str], collaborators: set[str]) -> None:
    case_id = str(case.get("id", "<no id>"))
    if case.get("kind") not in kinds:
        raise ValueError(f"{case_id}: unknown case kind {case.get('kind')!r}")
    named = [str(entry.get("collaborator")) for entry in case.get("collaborator_script", [])]
    if case.get("failing_collaborator") is not None:
        named.append(str(case["failing_collaborator"]))
    unknown = sorted(set(named) - collaborators)
    if unknown:
        raise ValueError(f"{case_id}: unknown collaborator(s) {unknown}")
    if any("failure" in entry for entry in case.get("collaborator_script", [])):
        raise ValueError(f"{case_id}: a failure belongs in 'failure', not in the script")
    if case.get("provenance") != "synthetic":
        raise ValueError(f"{case_id}: provenance must be 'synthetic'")
    if not any(key in case for key in DELIVERY_KEYS):
        raise ValueError(f"{case_id}: case carries neither a payload nor an event")
    if "event" in case and not case.get("reason"):
        raise ValueError(f"{case_id}: a direct event case must say why no adapter produces it")
    _validate_delivery(case_id, case, "the case delivery")
    for index, setup in enumerate(case.get("setup", [])):
        _validate_delivery(case_id, setup_delivery(case, setup), f"setup delivery {index}")
    if case["kind"] == "collaborator_failure" and not (case.get("failure") or {}).get("method"):
        raise ValueError(f"{case_id}: a collaborator failure must name the failing method")
    for rule in case.get("rules", []):
        name = str(rule.get("name", ""))
        if not name.startswith(SYNTHETIC_RULE_PREFIX):
            raise ValueError(
                f"{case_id}: rule name {name!r} must start with {SYNTHETIC_RULE_PREFIX!r}"
            )


def load_corpus(corpus_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(manifest, cases)`` for a pipeline corpus directory.

    Raises ``ValueError`` naming the offender for a stale ``schema_version``, an unknown
    case kind or an unknown collaborator name.
    """
    manifest_path = corpus_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    version = manifest.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{manifest_path.name}: schema_version {version!r} is not {SCHEMA_VERSION}"
        )
    kinds = {str(kind) for kind in manifest.get("case_kinds", [])}
    collaborators = {str(name) for name in manifest.get("collaborators", [])}
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted((corpus_dir / "cases").glob("*.json")):
        for case in json.loads(path.read_text())["cases"]:
            _validate_case(case, kinds, collaborators)
            if case["id"] in seen:
                raise ValueError(f"duplicate case id {case['id']!r}")
            seen.add(case["id"])
            cases.append(case)
    return manifest, cases


def check_coverage(corpus_dir: Path, axis: str) -> None:
    """Raise ``ValueError`` naming each required item on ``axis`` that no case carries."""
    from tests.contracts import pipeline_coverage

    pipeline_coverage.check_coverage(corpus_dir, axis)


def replay_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the Python ``HookManager.handle`` and return the observed output.

    Each call builds a fresh ``HookManager`` and ``EventHandlers``, so a class attribute patched
    before the call is the one the replay runs.
    """
    from tests.contracts import pipeline_recorder

    return pipeline_recorder.replay_case(case)


def record_corpus(corpus_dir: Path) -> None:
    """Fill each case's ``expect`` block when ``GOBBY_CORPUS_RECORD`` is 1; otherwise do nothing."""
    from tests.contracts import pipeline_recorder

    pipeline_recorder.record_corpus(corpus_dir, write_document)


def write_document(path: Path, document: dict[str, Any]) -> None:
    """Write one case file in the committed layout."""
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
