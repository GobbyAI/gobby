"""Bounded logging and automation health metric emission."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from gobby.telemetry.instruments import get_telemetry_metrics

if TYPE_CHECKING:
    from gobby.telemetry.instruments import TelemetryMetrics

_LOG_SURFACES: Final = frozenset({"daemon", "hooks", "llm", "mcp", "automation", "parser"})
_LOG_SEVERITIES: Final = frozenset({"WARNING", "ERROR", "CRITICAL"})
_AUTOMATION_OUTCOMES: Final = {
    "cron": frozenset({"fired", "succeeded", "failed"}),
    "dispatcher": frozenset({"succeeded", "failed", "skipped"}),
    "pipeline-heartbeat": frozenset({"recovered", "failed"}),
}

_metrics: TelemetryMetrics | None = None


def configure_health_metrics(*, enabled: bool) -> None:
    """Enable health emission after meter-provider initialization."""
    global _metrics

    metrics_instance: TelemetryMetrics | None = None
    if enabled:
        try:
            metrics_instance = get_telemetry_metrics()
        except Exception:
            # Telemetry must never prevent daemon startup.
            pass
    _metrics = metrics_instance

    from gobby.telemetry.logging import configure_logging_metrics

    configure_logging_metrics(enabled=metrics_instance is not None)


def record_logging_record(surface: str, severity: str) -> None:
    """Count one bounded WARNING+ source record."""
    if surface not in _LOG_SURFACES or severity not in _LOG_SEVERITIES:
        return
    _emit_counter(
        "logging_records_total",
        attributes={"surface": surface, "severity": severity},
    )


def record_automation_event(component: str, outcome: str, *, amount: int = 1) -> None:
    """Count bounded automation state transitions."""
    allowed_outcomes = _AUTOMATION_OUTCOMES.get(component)
    if allowed_outcomes is None or outcome not in allowed_outcomes or amount <= 0:
        return
    _emit_counter(
        "automation_events_total",
        amount=amount,
        attributes={"component": component, "outcome": outcome},
    )


def _emit_counter(
    name: str,
    *,
    amount: int = 1,
    attributes: dict[str, str],
) -> None:
    metrics_instance = _metrics
    if metrics_instance is None:
        return
    try:
        metrics_instance.inc_counter(name, amount=amount, attributes=attributes)
    except Exception:
        # Logging here could recurse through the logging metric handler.
        pass
