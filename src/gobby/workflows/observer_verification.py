"""Verification-evidence observer for workflow session variables."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.config.validation_detection import (
    ValidationCommandMatch,
    classify_validation_command,
    resolve_validation_detection_config,
)
from gobby.hooks.normalization import _SHELL_TOOLS
from gobby.workflows.observer_utils import (
    _extract_shell_command,
    _json_safe,
    _shell_tool_outcome,
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


def _validation_segment_outcome(
    match: ValidationCommandMatch,
    aggregate_outcome: bool | None,
) -> bool | None:
    """Resolve aggregate shell status to the matched validation segment."""
    if match.evidence_requires_confirmation:
        return None
    if not match.is_compound:
        return aggregate_outcome
    if (
        aggregate_outcome is True
        and match.shell_operators
        and all(operator == "&&" for operator in match.shell_operators)
    ):
        return True
    return None


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

    outcome = _shell_tool_outcome(event)
    success = _validation_segment_outcome(match, outcome.succeeded)
    evidence = {
        "evidence_type": VERIFICATION_EVIDENCE_TYPE_VALIDATION_COMMAND,
        "command": command,
        "cwd": event.cwd,
        "project_path": event.metadata.get("project_path"),
        "matcher_id": match.matcher_id,
        "matcher_label": match.label,
        "categories": list(match.categories),
        "languages": list(match.languages),
        "normalized_command": match.normalized_command,
        "normalized_argv": list(match.normalized_argv),
        "wrapper_chain": list(match.wrapper_chain),
        "segment_index": match.segment_index,
        "segment_count": match.segment_count,
        "shell_operators": list(match.shell_operators),
        "evidence_requires_confirmation": match.evidence_requires_confirmation,
        "timestamp": datetime.now(UTC).isoformat(),
        "tool_name": tool_name,
        "success": success,
    }
    if outcome.exit_code is not None:
        evidence["exit_code"] = outcome.exit_code
    if outcome.provenance is not None:
        evidence["outcome_provenance"] = outcome.provenance
    existing = variables.get(VERIFICATION_EVIDENCE_VARIABLE, [])
    variables[VERIFICATION_EVIDENCE_VARIABLE] = append_verification_evidence(
        existing, _json_safe(evidence), session_id=session_id
    )

    if success is True:
        variables[VERIFICATION_EVIDENCE_RECORDED_VARIABLE] = True
        logger.debug(
            "Session %s: verification_evidence_recorded=true via validation command",
            session_id,
        )
        return

    if success is False:
        variables[VERIFICATION_EVIDENCE_RECORDED_VARIABLE] = False
        logger.info(
            "Session %s: verification readiness cleared after failed validation command",
            session_id,
        )
        return

    logger.debug(
        "Session %s: verification readiness unchanged after validation command with unknown outcome",
        session_id,
    )
