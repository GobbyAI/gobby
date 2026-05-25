"""Verification-evidence observer for workflow session variables."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.config.validation_detection import (
    classify_validation_command,
    resolve_validation_detection_config,
)
from gobby.hooks.normalization import _SHELL_TOOLS
from gobby.workflows.observer_utils import (
    _extract_shell_command,
    _json_safe,
    _shell_tool_succeeded,
)
from gobby.workflows.verification_evidence import (
    VERIFICATION_EVIDENCE_RECORDED_VARIABLE,
    VERIFICATION_EVIDENCE_TYPE_VALIDATION_COMMAND,
    VERIFICATION_EVIDENCE_VARIABLE,
    append_verification_evidence,
)

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent

logger = logging.getLogger("gobby.workflows.observers")


def detect_verification_evidence(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
    daemon_config: Any | None = None,
) -> None:
    """Record validation-command evidence from shell tool runs."""
    if not event.data:
        return

    tool_name = event.data.get("tool_name", "")
    if tool_name not in _SHELL_TOOLS:
        return

    command = _extract_shell_command(event)
    detection_config = resolve_validation_detection_config(
        daemon_config=daemon_config,
        project_path=event.metadata.get("project_path") or event.cwd,
    )
    match = classify_validation_command(command, detection_config)
    if match is None:
        return

    success = _shell_tool_succeeded(event)
    evidence = {
        "evidence_type": VERIFICATION_EVIDENCE_TYPE_VALIDATION_COMMAND,
        "command": command,
        "cwd": event.cwd,
        "project_path": event.metadata.get("project_path"),
        "matcher_id": match.matcher_id,
        "matcher_label": match.label,
        "categories": list(match.categories),
        "languages": list(match.languages),
        "timestamp": datetime.now(UTC).isoformat(),
        "tool_name": tool_name,
        "success": success,
    }
    exit_code = _extract_exit_code(event)
    if exit_code is not None:
        evidence["exit_code"] = exit_code
    existing = variables.get(VERIFICATION_EVIDENCE_VARIABLE, [])
    variables[VERIFICATION_EVIDENCE_VARIABLE] = append_verification_evidence(
        existing, _json_safe(evidence), session_id=session_id
    )

    if success:
        variables[VERIFICATION_EVIDENCE_RECORDED_VARIABLE] = True
        logger.info(
            "Session %s: verification_evidence_recorded=true via validation command",
            session_id,
        )
        return

    variables[VERIFICATION_EVIDENCE_RECORDED_VARIABLE] = False
    logger.info(
        "Session %s: verification readiness cleared after failed validation command",
        session_id,
    )


def _extract_exit_code(event: HookEvent) -> int | None:
    if not event.data:
        return None
    output = event.data.get("tool_output")
    if not isinstance(output, dict):
        return None
    for key in ("exitCode", "exit_code", "returncode"):
        value = output.get(key)
        if isinstance(value, int):
            return value
    return None
