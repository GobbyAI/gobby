"""Results for the four task-close invariants."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

GateStatus = Literal["passed", "failed", "skipped", "not_run"]


@dataclass(frozen=True)
class CloseGateResult:
    """Result of one ordered close-checklist item."""

    item: int
    name: str
    status: GateStatus
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status != "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "name": self.name,
            "status": self.status,
            "passed": self.passed,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class CloseChecklist:
    """Every deterministic gate result in checklist order."""

    gates: tuple[CloseGateResult, ...]

    @property
    def ready(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def first_failure(self) -> CloseGateResult | None:
        return next((gate for gate in self.gates if not gate.passed), None)

    @property
    def all_failures(self) -> tuple[CloseGateResult, ...]:
        """Every failed gate, so one response names every blocker the caller must fix."""
        return tuple(gate for gate in self.gates if not gate.passed)

    def summary(self) -> list[dict[str, Any]]:
        """Gate statuses without the detail payloads, which run to tens of kilobytes."""
        return [
            {
                "item": gate.item,
                "name": gate.name,
                "status": gate.status,
                "message": gate.message,
            }
            for gate in self.gates
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "gates": [gate.to_dict() for gate in self.gates],
        }
