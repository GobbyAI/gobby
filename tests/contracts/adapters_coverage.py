"""Coverage check for the adapter corpus.

Required items are introspected from production, not read from the corpus: the hook-type axis
is each adapter's ``EVENT_MAP`` (inbound cases), and the response-field axis is every field a
source's capability table declares plus the adapter-owned fields (outbound cases). An
adapter-owned field counts only when a case sets it to a non-null value the adapter emits. The
check raises ``ValueError`` naming every required item that no case carries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.adapters.capabilities import get_provider_capabilities
from tests.contracts import adapters_corpus
from tests.contracts.adapters_recorder import ADAPTERS

AXES = ("hook_type", "response_field")
# Response fields the adapter layer sets, not the rule engine. Each is covered only by an
# outbound case whose non-null value the adapter emits: its recorded output differs from the
# replay of the same case with the field removed. A field no routed adapter emits is unrouted
# and is guarded in the test file instead of being required here.
ADAPTER_OWNED_RESPONSE_FIELDS: tuple[str, ...] = ("system_message",)
HOOK_SOURCES = ("claude", "codex", "droid", "grok", "agy")


def _declared_fields(source: str) -> set[str]:
    fields: set[str] = set()
    for hook in get_provider_capabilities(source).hook_events.values():
        fields |= set(hook.supported_response_fields)
    return fields


def _emits_field(case: dict[str, Any], field: str) -> bool:
    """True when an outbound case sets ``field`` to a non-null value the adapter emits."""
    response = case.get("response", {})
    if case.get("kind") != "outbound" or response.get(field) is None:
        return False
    stripped = {key: value for key, value in response.items() if key != field}
    replayed = adapters_corpus.replay_case({**case, "response": stripped})
    return bool(replayed.get("output") != case.get("expect", {}).get("output"))


def _missing(cases: list[dict[str, Any]], axis: str) -> list[str]:
    missing: list[str] = []
    if axis == "hook_type":
        for source in HOOK_SOURCES:
            hook_types = {
                str(case.get("hook_type"))
                for case in cases
                if case.get("kind") == "inbound" and case.get("source") == source
            }
            missing += [
                f"{source} {hook}"
                for hook in sorted(ADAPTERS[source].EVENT_MAP)
                if hook not in hook_types
            ]
        return missing
    for source in HOOK_SOURCES:
        carried: set[str] = set()
        for case in cases:
            if case.get("kind") == "outbound" and case.get("source") == source:
                carried |= {str(field) for field in case.get("response", {})}
        missing += [f"{source} {field}" for field in sorted(_declared_fields(source) - carried)]
    missing += [
        field
        for field in ADAPTER_OWNED_RESPONSE_FIELDS
        if not any(_emits_field(case, field) for case in cases)
    ]
    return missing


def check_coverage(corpus_dir: Path, axis: str) -> None:
    """Raise ``ValueError`` naming each required item on ``axis`` that no case carries."""
    if axis not in AXES:
        raise ValueError(f"unknown coverage axis {axis!r}")
    _manifest, cases = adapters_corpus.load_corpus(corpus_dir)
    missing = _missing(cases, axis)
    if missing:
        raise ValueError(f"{axis} coverage: no case carries {missing}")
