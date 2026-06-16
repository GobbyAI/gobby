"""Heartbeat result types and counters."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HeartbeatResult:
    scanned: int = 0
    executed: int = 0
    skipped: int = 0
    cap_reached: bool = False
    reason: str | None = None


def skipped(result: HeartbeatResult) -> HeartbeatResult:
    return HeartbeatResult(
        result.scanned,
        result.executed,
        result.skipped + 1,
        result.cap_reached,
        result.reason,
    )


def cap_reached(result: HeartbeatResult) -> HeartbeatResult:
    return HeartbeatResult(
        scanned=result.scanned,
        executed=result.executed,
        skipped=result.skipped,
        cap_reached=True,
        reason=result.reason,
    )


def action_cap_reached(result: HeartbeatResult, max_actions: int | None) -> bool:
    return max_actions is not None and result.executed >= max_actions


def unavailable(result: HeartbeatResult, reason: str) -> HeartbeatResult:
    return HeartbeatResult(result.scanned, result.executed, result.skipped + 1, False, reason)
