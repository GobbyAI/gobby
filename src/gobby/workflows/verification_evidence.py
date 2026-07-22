"""Helpers for verification evidence session variables."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MAX_VERIFICATION_EVIDENCE_ITEMS = 50
VERIFICATION_EVIDENCE_VARIABLE = "verification_evidence"
VERIFICATION_EVIDENCE_RECORDED_VARIABLE = "verification_evidence_recorded"
VERIFICATION_EVIDENCE_TYPE_VALIDATION_COMMAND = "validation_command"
VERIFICATION_EVIDENCE_TYPE_MANUAL_DIFF_REVIEW = "manual_diff_review"
VERIFICATION_EVIDENCE_RESET_UPDATES = {
    VERIFICATION_EVIDENCE_RECORDED_VARIABLE: False,
    VERIFICATION_EVIDENCE_VARIABLE: [],
}

CommandResultSignal = Literal["exit_code", "provider_status"]

_TRUSTED_PROVIDER_OUTCOME_RE = re.compile(
    r"^(?:claude|codex|droid|grok|qwen)\.(?:hook|stream|structured):[^\s]+$"
)
_TRUSTED_STRUCTURED_OUTCOME_RE = re.compile(
    r"^(?:adapter|event|hook_event|tool_outcome|tool_output|tool_result|tool_response|"
    r"contentItems)(?:[.\[].*)?\."
    r"(?:exit_code|exitCode|status|success|isError|is_error|is_failure)$"
)


@dataclass(frozen=True)
class CorrelatedCommandResult:
    """A terminal command result backed by a trusted machine-derived signal."""

    success: bool
    signal: CommandResultSignal


def correlate_validation_command_result(
    evidence: Mapping[str, Any],
) -> CorrelatedCommandResult | None:
    """Correlate an exact command with one consistent, trusted terminal outcome."""
    command = evidence.get("command")
    success = evidence.get("success")
    provenance = evidence.get("outcome_provenance")
    if (
        not isinstance(command, str)
        or not command.strip()
        or not isinstance(success, bool)
        or not _is_trusted_outcome_provenance(provenance)
    ):
        return None

    exit_code = evidence.get("exit_code")
    if isinstance(exit_code, bool):
        return None
    if isinstance(exit_code, int):
        exit_success = exit_code == 0
        if exit_success is not success:
            return None
        return CorrelatedCommandResult(success=success, signal="exit_code")
    if exit_code is not None:
        return None
    return CorrelatedCommandResult(success=success, signal="provider_status")


def _is_trusted_outcome_provenance(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value == "adapter.explicit_tool_outcome":
        return True
    return bool(
        _TRUSTED_PROVIDER_OUTCOME_RE.fullmatch(value)
        or _TRUSTED_STRUCTURED_OUTCOME_RE.fullmatch(value)
    )


_EVIDENCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_:-]{1,63}$")
logger = logging.getLogger(__name__)


class VerificationEvidence(BaseModel):
    """Typed verification evidence item stored in session variables."""

    model_config = ConfigDict(extra="forbid")

    evidence_type: str = Field(strict=True)
    success: bool | None = Field(strict=True)
    timestamp: str | None = Field(default=None, strict=True)
    command: str | None = Field(default=None, strict=True)
    summary: str | None = Field(default=None, strict=True)
    supports: str | None = Field(default=None, strict=True)
    task_id: str | None = Field(default=None, strict=True)
    stage_name: str | None = Field(default=None, strict=True)
    scope: str | None = Field(default=None, strict=True)
    cwd: str | None = Field(default=None, strict=True)
    project_path: str | None = Field(default=None, strict=True)
    matcher_id: str | None = Field(default=None, strict=True)
    matcher_label: str | None = Field(default=None, strict=True)
    categories: list[str] | None = None
    languages: list[str] | None = None
    normalized_command: str | None = Field(default=None, strict=True)
    normalized_argv: list[str] | None = None
    wrapper_chain: list[str] | None = None
    segment_index: int | None = Field(default=None, ge=0, strict=True)
    segment_count: int | None = Field(default=None, ge=1, strict=True)
    shell_operators: list[str] | None = None
    evidence_requires_confirmation: bool | None = Field(default=None, strict=True)
    tool_name: str | None = Field(default=None, strict=True)
    exit_code: int | None = Field(default=None, strict=True)
    outcome_provenance: str | None = Field(default=None, strict=True)

    @field_validator("evidence_type")
    @classmethod
    def _validate_evidence_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("requires a non-empty evidence_type")
        value = value.strip()
        if not _EVIDENCE_TYPE_RE.fullmatch(value):
            raise ValueError("evidence_type must be snake-case text")
        return value

    @model_validator(mode="after")
    def _validate_command_or_summary(self) -> VerificationEvidence:
        if not self.command and not self.summary:
            raise ValueError("requires command or summary")
        return self


def append_verification_evidence(
    existing: list[Any] | None,
    evidence: dict[str, Any],
    *,
    session_id: str | None = None,
) -> list[Any]:
    """Append one evidence item, retaining only the newest entries."""
    validated = _validate_verification_evidence_model(evidence)
    if isinstance(existing, list):
        items = existing
    elif existing is None:
        items = []
    else:
        logger.warning(
            "Ignoring malformed verification_evidence value",
            extra={"stored_type": type(existing).__name__, "session_id": session_id},
        )
        items = []
    serialized = validated.model_dump(mode="json", exclude_none=True)
    if validated.success is None:
        serialized["success"] = None
    return [*items, serialized][-MAX_VERIFICATION_EVIDENCE_ITEMS:]


def validate_verification_evidence(evidence: Mapping[str, Any]) -> str | None:
    """Return a validation error for malformed verification evidence, if any."""
    try:
        _validate_verification_evidence_model(evidence)
    except ValueError as exc:
        return str(exc)

    return None


def _validate_verification_evidence_model(
    evidence: Mapping[str, Any],
) -> VerificationEvidence:
    try:
        return VerificationEvidence.model_validate(dict(evidence))
    except ValidationError as exc:
        extra_fields = sorted(
            str(error.get("loc", ("",))[0])
            for error in exc.errors()
            if error.get("type") == "extra_forbidden" and error.get("loc")
        )
        if extra_fields:
            raise ValueError(
                f"verification evidence contains unsupported fields: {', '.join(extra_fields)}"
            ) from exc
        for error in exc.errors():
            loc = error.get("loc", ())
            if loc == ("evidence_type",):
                message = str(error.get("msg", ""))
                if "snake-case text" in message:
                    raise ValueError(
                        "verification evidence evidence_type must be snake-case text"
                    ) from exc
                raise ValueError(
                    "verification evidence requires a non-empty evidence_type"
                ) from exc
            if loc == ("success",):
                raise ValueError("verification evidence requires a boolean success field") from exc
            if loc == ("timestamp",):
                raise ValueError(
                    "verification evidence timestamp must be a string when provided"
                ) from exc
            if loc == ("command",):
                raise ValueError(
                    "verification evidence command must be a string when provided"
                ) from exc
            if loc == ("summary",):
                raise ValueError(
                    "verification evidence summary must be a string when provided"
                ) from exc
        raise ValueError("verification evidence requires command or summary") from exc
