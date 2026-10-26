"""Shared parity waiver manifest: one loader for ledger generation, corpus replay and compare readiness.

Behaviour-free stub for the test-first commit. The loader, validation and consumers land in the
developer's implementation commit.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WAIVERS_PATH = ROOT / "docs/contracts/parity-waivers.json"

WAIVED = "Waived"
NATIVE_EXTENSION = "native-extension"
PASSED = "Passed"
DIFF = "Diff"


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


def load_waivers(
    path: Path = WAIVERS_PATH,
    *,
    surfaces: AbstractSet[str],
    additive_surfaces: AbstractSet[str] = frozenset(),
) -> dict[str, Waiver]:
    """Stub: returns no waivers and performs no validation."""
    return {}
