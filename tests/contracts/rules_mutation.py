"""The shared rules for a replay made with a production path replaced by a stub.

The recorder turns every exception into ``outcome_class: "error"`` and a ``raised``
effect-log entry. A stub with a wrong signature would therefore change the recorded
outcome without the path ever being replaced by a working constant, so every stubbed
replay is checked here before its outcome is compared. What counts as an outcome change
is one projection, ``produced_outputs``, used by every mutation and twin comparison.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SEAM_CALLER_KIND = "seam_caller"
# What a pass produces. The probe log (reached, between_reached), the eval-context facts,
# the fault hit log and the observer failures are diagnostics: recorded and replayed
# exactly, but never counted as an outcome change under a stub.
PRODUCED_PASS_KEYS = (
    "outcome_class",
    "response",
    "effect_log",
    "audit_records",
    "eager_variable_delta",
    "eager_variable_deletions",
    "observe",
)
# What a seam caller produces: the value the caller returns, never the seam effect log,
# which is the recorder's own wrapper log.
PRODUCED_CALLER_KEYS = ("outcome_class", "returned", "audit_records")


def produced_pass(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the produced outputs of one pass entry (or of a whole-case top level)."""
    return {key: entry[key] for key in PRODUCED_PASS_KEYS if key in entry}


def _caller_returned(case: Mapping[str, Any], returned: Any) -> Any:
    """Drop the keys the case declares as a harness-written log from a mapping ``returned``."""
    spy_keys = case.get("returned_spy_keys")
    if not isinstance(returned, Mapping) or not isinstance(spy_keys, list):
        return returned
    return {key: value for key, value in returned.items() if key not in spy_keys}


def produced_outputs(case: Mapping[str, Any], expect: Mapping[str, Any]) -> dict[str, Any]:
    """Return the one projection a stubbed replay is compared on.

    A ``rule_pass`` or ``handler_pass`` case projects its top level and every pass onto
    ``PRODUCED_PASS_KEYS``. A ``seam_caller`` case has no passes; its top level is projected
    onto ``PRODUCED_CALLER_KEYS``, with the case's ``returned_spy_keys`` removed from a
    mapping ``returned``.
    """
    if case.get("kind") == SEAM_CALLER_KIND:
        top = {key: expect[key] for key in PRODUCED_CALLER_KEYS if key in expect}
        if "returned" in top:
            top["returned"] = _caller_returned(case, top["returned"])
        return {"top": top, "passes": []}
    passes = expect.get("passes", [])
    entries = passes if isinstance(passes, list) else []
    return {
        "top": produced_pass(expect),
        "passes": [produced_pass(entry) for entry in entries if isinstance(entry, Mapping)],
    }


def _sections(expect: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    yield "top level", expect
    passes = expect.get("passes", [])
    if isinstance(passes, list):
        for index, entry in enumerate(passes):
            if isinstance(entry, Mapping):
                yield f"pass {index}", entry


def assert_clean_stubbed_replay(expect: Mapping[str, Any], label: str) -> None:
    """Raise ``AssertionError`` naming ``label`` when the stubbed replay raised anywhere.

    A replay raised when its top-level or any pass's ``outcome_class`` is ``"error"``, or
    when any effect-log entry, top level or per pass, carries a ``raised`` key.
    """
    problems: list[str] = []
    for where, section in _sections(expect):
        if section.get("outcome_class") == "error":
            problems.append(f"{where}: outcome_class is 'error'")
        log = section.get("effect_log", [])
        if isinstance(log, list):
            raised = [
                entry["raised"] for entry in log if isinstance(entry, Mapping) and "raised" in entry
            ]
            if raised:
                problems.append(f"{where}: effect_log raised {raised}")
    if problems:
        raise AssertionError(f"{label}: the stubbed replay raised: {'; '.join(problems)}")
