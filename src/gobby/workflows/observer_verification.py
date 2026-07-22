"""Provider-neutral shell-result evidence observer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gobby.hooks.events import HookEvent
from gobby.hooks.normalization import _SHELL_TOOLS
from gobby.workflows.observer_utils import (
    _extract_shell_command,
    _extract_shell_output_text,
    _json_safe,
    _shell_tool_outcome,
)
from gobby.workflows.verification_evidence import (
    VERIFICATION_EVIDENCE_RECORDED_VARIABLE,
    VERIFICATION_EVIDENCE_TYPE_SHELL_COMMAND,
    VERIFICATION_EVIDENCE_VARIABLE,
    append_verification_evidence,
)

_OUTPUT_LIMIT = 8_192


def _extract_shell_output(event: HookEvent) -> str:
    for field in ("tool_output", "tool_result", "tool_response", "contentItems"):
        output = _extract_shell_output_text(event.data.get(field))
        if output:
            return output
    return ""


def _bounded_output(output: str) -> str:
    if len(output) <= _OUTPUT_LIMIT:
        return output
    half = _OUTPUT_LIMIT // 2
    return output[:half] + "\n... output elided ...\n" + output[-half:]


def detect_verification_evidence(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
    daemon_config: Any | None = None,
) -> None:
    """Record one canonical shell outcome without interpreting command semantics."""
    del daemon_config
    if not event.data or event.data.get("tool_name") not in _SHELL_TOOLS:
        return
    command = _extract_shell_command(event)
    if not command:
        return

    outcome = _shell_tool_outcome(event)
    evidence = {
        "evidence_type": VERIFICATION_EVIDENCE_TYPE_SHELL_COMMAND,
        "command": command,
        "cwd": event.cwd,
        "project_path": event.metadata.get("project_path"),
        "timestamp": datetime.now(UTC).isoformat(),
        "tool_name": event.data.get("tool_name"),
        "success": outcome.succeeded,
        "output": _bounded_output(_extract_shell_output(event)),
    }
    if outcome.exit_code is not None:
        evidence["exit_code"] = outcome.exit_code
    if outcome.provenance is not None:
        evidence["outcome_provenance"] = outcome.provenance

    existing = variables.get(VERIFICATION_EVIDENCE_VARIABLE, [])
    variables[VERIFICATION_EVIDENCE_VARIABLE] = append_verification_evidence(
        existing,
        _json_safe(evidence),
        session_id=session_id,
    )

    from gobby.workflows.condition_helpers import completion_evidence_ready

    variables[VERIFICATION_EVIDENCE_RECORDED_VARIABLE] = completion_evidence_ready(variables)


__all__ = ["detect_verification_evidence"]
