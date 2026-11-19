"""Coverage check for the adapter corpus.

Required items are introspected from production, not read from the corpus: the hook-type axis
is each adapter's ``EVENT_MAP`` (inbound cases), and the response-field axis is every field a
source's capability table declares plus the adapter-owned fields (outbound cases). The check
raises ``ValueError`` naming every required item that no case carries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.adapters.capabilities import get_provider_capabilities
from tests.contracts import adapters_corpus
from tests.contracts.adapters_recorder import ADAPTERS

AXES = ("hook_type", "response_field")
# Response fields the adapter layer sets, not the rule engine; no capability row lists
# modify_args, so the field is required on its own.
ADAPTER_OWNED_RESPONSE_FIELDS = ("system_message", "modify_args")
HOOK_SOURCES = ("claude", "codex", "droid", "grok", "agy")


def _declared_fields(source: str) -> set[str]:
    fields: set[str] = set()
    for hook in get_provider_capabilities(source).hook_events.values():
        fields |= set(hook.supported_response_fields)
    return fields


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
    every: set[str] = set()
    for source in HOOK_SOURCES:
        carried: set[str] = set()
        for case in cases:
            if case.get("kind") == "outbound" and case.get("source") == source:
                carried |= {str(field) for field in case.get("response", {})}
        every |= carried
        missing += [f"{source} {field}" for field in sorted(_declared_fields(source) - carried)]
    missing += [field for field in ADAPTER_OWNED_RESPONSE_FIELDS if field not in every]
    return missing


def check_coverage(corpus_dir: Path, axis: str) -> None:
    """Raise ``ValueError`` naming each required item on ``axis`` that no case carries."""
    if axis not in AXES:
        raise ValueError(f"unknown coverage axis {axis!r}")
    _manifest, cases = adapters_corpus.load_corpus(corpus_dir)
    missing = _missing(cases, axis)
    if missing:
        raise ValueError(f"{axis} coverage: no case carries {missing}")
