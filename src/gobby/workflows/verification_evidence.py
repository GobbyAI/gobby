"""Helpers for verification evidence session variables."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from gobby.tasks.verification_outcome_projection import VerificationOutcomeProjection

MAX_VERIFICATION_EVIDENCE_ITEMS = 50
VERIFICATION_EVIDENCE_VARIABLE = "verification_evidence"
VERIFICATION_EVIDENCE_RECORDED_VARIABLE = "verification_evidence_recorded"
VERIFICATION_EVIDENCE_TYPE_SHELL_COMMAND = "shell_command"
VERIFICATION_EVIDENCE_TYPE_RECEIPT_PROJECTION = "receipt_projection"
VERIFICATION_EVIDENCE_TYPE_MANUAL_DIFF_REVIEW = "manual_diff_review"
VERIFICATION_EVIDENCE_RESET_UPDATES = {
    VERIFICATION_EVIDENCE_RECORDED_VARIABLE: False,
    VERIFICATION_EVIDENCE_VARIABLE: [],
}

_EVIDENCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_:-]{1,63}$")
logger = logging.getLogger(__name__)


def receipt_projection_evidence(
    projection: VerificationOutcomeProjection,
    *,
    task_id: str,
) -> dict[str, Any]:
    counts = {key: projection.per_outcome[key] for key in sorted(projection.per_outcome)}
    return {
        "evidence_type": VERIFICATION_EVIDENCE_TYPE_RECEIPT_PROJECTION,
        "success": projection.ready,
        "timestamp": (
            projection.latest_timestamp.isoformat()
            if projection.latest_timestamp is not None
            else None
        ),
        "summary": f"Durable verification receipt outcomes: {counts}",
        "task_id": task_id,
        "outcome_counts": counts,
        "receipt_count": projection.total,
        "latest_receipt_id": projection.latest_receipt_id,
    }


def merge_receipt_projection_evidence(
    existing: Any,
    projection: VerificationOutcomeProjection,
    *,
    task_id: str,
    session_id: str,
) -> list[dict[str, Any]]:
    items = existing if isinstance(existing, list) else []
    retained = [
        item
        for item in items
        if not (
            isinstance(item, Mapping)
            and item.get("evidence_type") == VERIFICATION_EVIDENCE_TYPE_RECEIPT_PROJECTION
            and item.get("task_id") == task_id
        )
    ]
    return append_verification_evidence(
        retained,
        receipt_projection_evidence(projection, task_id=task_id),
        session_id=session_id,
    )


class VerificationEvidence(BaseModel):
    """Typed verification evidence item stored in session variables."""

    model_config = ConfigDict(extra="forbid")

    evidence_type: str = Field(strict=True)
    success: bool | None = Field(strict=True)
    timestamp: str | None = Field(default=None, strict=True)
    command: str | None = Field(default=None, strict=True)
    output: str | None = Field(default=None, strict=True)
    summary: str | None = Field(default=None, strict=True)
    supports: str | None = Field(default=None, strict=True)
    task_id: str | None = Field(default=None, strict=True)
    stage_name: str | None = Field(default=None, strict=True)
    scope: str | None = Field(default=None, strict=True)
    cwd: str | None = Field(default=None, strict=True)
    project_path: str | None = Field(default=None, strict=True)
    outcome_counts: dict[str, int] | None = None
    receipt_count: int | None = Field(default=None, ge=0, strict=True)
    latest_receipt_id: str | None = Field(default=None, strict=True)
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
