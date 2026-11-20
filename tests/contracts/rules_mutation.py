"""The shared rule for a replay made with a production path replaced by a stub.

The recorder turns every exception into ``outcome_class: "error"`` and a ``raised``
effect-log entry. A stub with a wrong signature would therefore change the recorded
outcome without the path ever being replaced by a working constant, so every stubbed
replay is checked here before its outcome is compared.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


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
