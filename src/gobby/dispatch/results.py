"""Heartbeat result types and counters."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class HeartbeatResult:
    scanned: int = 0
    executed: int = 0
    skipped: int = 0
    cap_reached: bool = False
    reason: str | None = None


def skipped(result: HeartbeatResult) -> HeartbeatResult:
    return replace(result, skipped=result.skipped + 1)


def cap_reached(result: HeartbeatResult) -> HeartbeatResult:
    return replace(result, cap_reached=True)


def action_cap_reached(result: HeartbeatResult, max_actions: int | None) -> bool:
    return max_actions is not None and result.executed >= max_actions


def unavailable(result: HeartbeatResult, reason: str) -> HeartbeatResult:
    return replace(result, skipped=result.skipped + 1, cap_reached=False, reason=reason)
