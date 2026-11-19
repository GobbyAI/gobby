"""Coverage check for the rule-pass corpus.

Each axis names the items the manifest requires (``manifest["required"][axis]``). The
check collects what the committed cases carry on that axis and raises ``ValueError``
naming every required item that no case carries. Required items live in the manifest,
so a corpus copy with some cases removed fails on the items those cases carried.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tests.contracts import rules_corpus, rules_recorder

AXES = ("scenario", "effect_type", "delivery", "response_field", "family", "observer", "transition")


def _carried(case: Mapping[str, Any], axis: str, defaults: Mapping[str, Any]) -> set[str]:
    if axis == "scenario":
        return set(case.get("scenario_ids", []))
    if axis == "effect_type":
        types = {effect["type"] for rule in case["rules"] for effect in rule["effects"]}
        types.update(entry["effect"] for entry in case["expect"]["effect_log"] if "effect" in entry)
        return types
    if axis == "delivery":
        deliveries = {
            effect.get("delivery", "eager") for rule in case["rules"] for effect in rule["effects"]
        }
        deliveries.update(
            entry["delivery"] for entry in case["expect"]["effect_log"] if "delivery" in entry
        )
        return deliveries
    if axis == "response_field":
        response = case["expect"].get(rules_corpus.RESPONSE_KEY, {})
        return {
            field
            for field, value in response.items()
            if field in defaults and value != defaults[field]
        }
    label = case.get(axis)
    return {label} if label else set()


def check_coverage(corpus_dir: Path, axis: str) -> None:
    """Raise ``ValueError`` naming each required item on ``axis`` that no case carries."""
    if axis not in AXES:
        raise ValueError(f"unknown coverage axis {axis!r}")
    manifest, cases = rules_corpus.load_corpus(corpus_dir)
    required = manifest.get("required", {}).get(axis)
    if not required:
        raise ValueError(f"the manifest names no required {axis} items")
    defaults = rules_recorder.response_defaults()
    carried: set[str] = set()
    for case in cases:
        carried |= _carried(case, axis, defaults)
    missing = [item for item in required if item not in carried]
    if missing:
        raise ValueError(f"{axis} coverage: no case carries {missing}")
