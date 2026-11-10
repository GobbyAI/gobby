"""Pending Codex shell-execution records shared by the correlator and its callers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class PendingExecution:
    """One original shell execution carried through wait and stdin polling."""

    outer_call_id: str
    literal_command: str | None = None
    cell_id: str | None = None
    session_id: str | None = None
    direct: bool = False
    expects_serialized_result: bool = False
    workdir: str | None = None
    # Set per registered call: a poll's mode can differ from the launch it continues.
    code_mode_call: bool = False

    def to_state(self) -> dict[str, Any]:
        return {
            "outer_call_id": self.outer_call_id,
            "literal_command": self.literal_command,
            "cell_id": self.cell_id,
            "session_id": self.session_id,
            "direct": self.direct,
            "expects_serialized_result": self.expects_serialized_result,
            "workdir": self.workdir,
            "code_mode_call": self.code_mode_call,
        }

    @classmethod
    def from_state(cls, value: Any) -> PendingExecution | None:
        if not isinstance(value, dict):
            return None
        outer_call_id = value.get("outer_call_id")
        if not isinstance(outer_call_id, str) or not outer_call_id:
            return None
        optional = {
            key: value.get(key) for key in ("literal_command", "cell_id", "session_id", "workdir")
        }
        if any(item is not None and not isinstance(item, str) for item in optional.values()):
            return None
        direct = value.get("direct", False)
        expects_serialized_result = value.get("expects_serialized_result", False)
        code_mode_call = value.get("code_mode_call", False)
        if not all(
            isinstance(flag, bool) for flag in (direct, expects_serialized_result, code_mode_call)
        ):
            return None
        return cls(
            outer_call_id=outer_call_id,
            direct=direct,
            expects_serialized_result=expects_serialized_result,
            code_mode_call=code_mode_call,
            **optional,
        )


@dataclass(frozen=True)
class ExecutionResolution:
    """Result of correlating one completed Codex tool-call output."""

    state: Literal["unrelated", "pending", "terminal", "unknown"]
    execution: PendingExecution | None = None
    results: tuple[dict[str, Any], ...] = ()
    reason: str | None = None
