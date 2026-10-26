"""Shared parity waiver manifest: one loader for ledger generation, corpus replay and compare readiness.

A waiver records one deliberate divergence inside a ported surface, or the approval of an additive
native-only surface. Consumers report a waived behaviour as Waived with its citation, never as
Passed, Skipped or masked.
"""

from __future__ import annotations

import json
import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WAIVERS_PATH = ROOT / "docs/contracts/parity-waivers.json"
SCHEMA_VERSION = 1

WAIVED = "Waived"
NATIVE_EXTENSION = "native-extension"
PASSED = "Passed"
DIFF = "Diff"

_FIELDS = frozenset({"id", "surface", "behaviour", "decision", "replacement"})
_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_ID_RE = re.compile(rf"{_SEGMENT}\.{_SEGMENT}\.{_SEGMENT}")
_DECISION_RE = re.compile(r"#[1-9][0-9]*|\.gobby/plans/[A-Za-z0-9._-]+\.md#[A-Za-z0-9._-]+")


class WaiverError(ValueError):
    """A waiver manifest entry, or its coverage of the inventory, is invalid."""


@dataclass(frozen=True)
class Waiver:
    id: str
    surface: str
    behaviour: str
    decision: str
    replacement: str

    @property
    def native_extension(self) -> bool:
        return self.id.endswith(".native-extension")


def _parse_entry(entry: object) -> Waiver:
    if not isinstance(entry, dict) or set(entry) != _FIELDS:
        raise WaiverError(
            "Waiver entries need exactly id, surface, behaviour, decision, replacement"
        )
    for field in sorted(_FIELDS):
        value = entry[field]
        if not isinstance(value, str) or not value.strip():
            raise WaiverError(f"Waiver field {field} must be a non-empty string")
    waiver = Waiver(**entry)
    if _ID_RE.fullmatch(waiver.id) is None:
        raise WaiverError(f"Waiver id {waiver.id!r} must be <family>.<surface>.<behaviour> slugs")
    if _DECISION_RE.fullmatch(waiver.decision) is None:
        raise WaiverError(f"{waiver.id}: decision must be #N or .gobby/plans/<plan>.md#<section>")
    return waiver


def _read_entries(path: Path) -> list[object]:
    try:
        document: Any = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise WaiverError(f"Cannot read waiver manifest {path}: {error}") from error
    if not isinstance(document, dict) or set(document) != {"schema_version", "waivers"}:
        raise WaiverError("Waiver manifest must be an object with schema_version and waivers")
    version = document["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise WaiverError(f"Waiver manifest schema_version must be {SCHEMA_VERSION}")
    if not isinstance(document["waivers"], list):
        raise WaiverError("Waiver manifest waivers must be a list")
    return document["waivers"]


def load_waivers(
    path: Path = WAIVERS_PATH,
    *,
    surfaces: AbstractSet[str],
    additive_surfaces: AbstractSet[str] = frozenset(),
) -> dict[str, Waiver]:
    """Validate every entry against the ledger inventory and return them keyed by id.

    A behaviour waiver must cite an exact inventory surface. A native-extension entry must cite
    a declared additive surface with no Python inventory row, and each additive surface needs
    exactly one such entry.
    """
    loaded: dict[str, Waiver] = {}
    native_surfaces: set[str] = set()
    for entry in _read_entries(path):
        waiver = _parse_entry(entry)
        if waiver.id in loaded:
            raise WaiverError(f"Duplicate waiver id {waiver.id!r}")
        if waiver.native_extension:
            if waiver.surface not in additive_surfaces:
                raise WaiverError(
                    f"{waiver.id}: {waiver.surface!r} is not a declared additive surface"
                )
            if waiver.surface in surfaces:
                raise WaiverError(
                    f"{waiver.id}: {waiver.surface!r} already has a Python inventory row"
                )
            if waiver.surface in native_surfaces:
                raise WaiverError(f"{waiver.surface!r} has more than one native-extension entry")
            native_surfaces.add(waiver.surface)
        elif waiver.surface not in surfaces:
            raise WaiverError(f"{waiver.id}: surface {waiver.surface!r} is not an exact ledger key")
        loaded[waiver.id] = waiver
    uncovered = sorted(set(additive_surfaces) - native_surfaces)
    if uncovered:
        raise WaiverError(
            f"Additive surfaces without a native-extension entry: {', '.join(uncovered)}"
        )
    return loaded
