"""SessionStart lifecycle and responsiveness diagnostics."""

from typing import Any

SLOW_SESSION_START_THRESHOLD_MS = 1000


def _log_session_start_lifecycle(
    handler: Any,
    *,
    session_id: str | None,
    session_source: str | None,
    cli_source: str,
    project_id: str | None,
    parent_session_id: str | None,
    pre_created: bool = False,
) -> None:
    parts = ["Session start:"]
    if session_id:
        parts.append(f"session={session_id}")
    if session_source:
        parts.append(f"source={session_source}")
    parts.append(f"cli={cli_source}")
    if project_id:
        parts.append(f"project={project_id}")
    if parent_session_id:
        parts.append(f"parent={parent_session_id}")
    if pre_created:
        parts.append("pre_created=true")
    handler.logger.debug(" ".join(parts))


def _log_session_start_timing(
    handler: Any,
    *,
    session_source: str,
    session_id: str | None,
    timings: dict[str, int],
) -> None:
    timing_message = f"SESSION_START timing [{session_source}]: " + " ".join(
        f"{name}={duration}ms" for name, duration in timings.items()
    )
    slow_component, slow_ms = max(
        ((name, duration) for name, duration in timings.items() if name != "total"),
        key=lambda item: item[1],
        default=("total", timings.get("total", 0)),
    )
    total_ms = timings.get("total", 0)
    if total_ms >= SLOW_SESSION_START_THRESHOLD_MS:
        handler.logger.info(
            "SESSION_START slow: component=%s duration=%sms total=%sms source=%s session=%s",
            slow_component,
            slow_ms,
            total_ms,
            session_source,
            session_id,
        )
    else:
        handler.logger.debug(timing_message)
