"""Canonical structured tool execution outcomes.

Provider adapters expose several result shapes. This module reduces their
machine-readable fields to one tri-state contract before workflow observers
consume hook events. Display text is deliberately excluded from inference.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any


class ToolOutcomeStatus(StrEnum):
    """Canonical tool execution state."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Machine-derived tool outcome plus its strongest source signal."""

    status: ToolOutcomeStatus
    exit_code: int | None = None
    provenance: str | None = None

    @property
    def succeeded(self) -> bool | None:
        if self.status is ToolOutcomeStatus.SUCCEEDED:
            return True
        if self.status is ToolOutcomeStatus.FAILED:
            return False
        return None

    def to_dict(self) -> dict[str, str | int]:
        result: dict[str, str | int] = {"status": self.status.value}
        if self.exit_code is not None:
            result["exit_code"] = self.exit_code
        if self.provenance is not None:
            result["provenance"] = self.provenance
        return result


@dataclass(frozen=True, slots=True)
class _OutcomeSignal:
    succeeded: bool
    provenance: str
    trust: _OutcomeTrust
    exit_code: int | None = None


class _OutcomeTrust(IntEnum):
    PROVIDER_CONTRACT = 0
    DIRECT_RESULT = 1
    NESTED_RESULT = 2


_EXIT_CODE_FIELDS = ("exitCode", "exit_code", "returncode")
_FAILURE_STATUSES = frozenset({"error", "failed", "failure"})
_SUCCESS_STATUSES = frozenset({"ok", "succeeded", "success"})
_TERMINAL_ONLY_STATUSES = frozenset(
    {"complete", "completed", "inprogress", "in_progress", "pending", "running"}
)
_OUTPUT_WRAPPER_FIELDS = (
    "structuredContent",
    "structured_content",
    "result",
    "content",
    "contentItems",
    "content_items",
    "output",
    "value",
)
_CONTENT_TEXT_FIELDS = ("text", "content")
_WRAPPER_TOOL_NAMES = frozenset({"exec", "functions.exec", "wait", "functions.wait"})
_MAX_OUTCOME_DEPTH = 10


def _normalized_tool_name(data: Mapping[str, Any]) -> str:
    for field in ("_original_tool_name", "original_tool_name", "tool_name"):
        value = data.get(field)
        if isinstance(value, str) and value:
            return value.strip().lower()
    return ""


def _parse_json_container(value: Any) -> Mapping[str, Any] | Sequence[Any] | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, Mapping):
        return parsed
    if isinstance(parsed, list):
        return parsed
    return None


def _canonical_outcome_signal(
    value: Any,
    path: str,
    trust: _OutcomeTrust,
) -> _OutcomeSignal | None:
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    if not isinstance(status, str):
        return None
    normalized = status.strip().lower()
    if normalized == ToolOutcomeStatus.SUCCEEDED.value:
        return _OutcomeSignal(True, str(value.get("provenance") or f"{path}.status"), trust)
    if normalized == ToolOutcomeStatus.FAILED.value:
        return _OutcomeSignal(False, str(value.get("provenance") or f"{path}.status"), trust)
    return None


def _collect_mapping_signals(
    value: Mapping[str, Any],
    path: str,
    signals: list[_OutcomeSignal],
    unknown_provenance: list[str],
    trust: _OutcomeTrust,
) -> None:
    canonical = _canonical_outcome_signal(value, path, trust)
    if canonical is not None:
        exit_code = value.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            canonical = _OutcomeSignal(
                canonical.succeeded,
                canonical.provenance,
                trust,
                exit_code,
            )
        signals.append(canonical)

    for field in _EXIT_CODE_FIELDS:
        exit_code = value.get(field)
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            signals.append(_OutcomeSignal(exit_code == 0, f"{path}.{field}", trust, exit_code))
            break

    is_error = value.get("isError", value.get("is_error"))
    if isinstance(is_error, bool):
        field = "isError" if "isError" in value else "is_error"
        signals.append(_OutcomeSignal(not is_error, f"{path}.{field}", trust))

    success = value.get("success")
    if isinstance(success, bool):
        signals.append(_OutcomeSignal(success, f"{path}.success", trust))

    status = value.get("status")
    if isinstance(status, str):
        normalized = status.strip().lower()
        if normalized in _FAILURE_STATUSES:
            signals.append(_OutcomeSignal(False, f"{path}.status", trust))
        elif normalized in _SUCCESS_STATUSES:
            signals.append(_OutcomeSignal(True, f"{path}.status", trust))
        elif normalized in _TERMINAL_ONLY_STATUSES and not unknown_provenance:
            unknown_provenance.append(f"{path}.status:{normalized}")


def _collect_output_signals(
    value: Any,
    path: str,
    signals: list[_OutcomeSignal],
    unknown_provenance: list[str],
    *,
    trust: _OutcomeTrust,
    depth: int = 0,
) -> None:
    if depth >= _MAX_OUTCOME_DEPTH:
        return

    parsed = _parse_json_container(value)
    if parsed is not None:
        _collect_output_signals(
            parsed,
            f"{path}.json",
            signals,
            unknown_provenance,
            trust=trust,
            depth=depth + 1,
        )
        return

    if isinstance(value, Mapping):
        _collect_mapping_signals(value, path, signals, unknown_provenance, trust)
        for field in _OUTPUT_WRAPPER_FIELDS:
            nested = value.get(field)
            if nested is None:
                continue
            _collect_output_signals(
                nested,
                f"{path}.{field}",
                signals,
                unknown_provenance,
                trust=_OutcomeTrust.NESTED_RESULT,
                depth=depth + 1,
            )
        block_type = value.get("type")
        if isinstance(block_type, str) and block_type.lower() in {
            "text",
            "inputtext",
            "input_text",
        }:
            for field in _CONTENT_TEXT_FIELDS:
                if field in value:
                    _collect_output_signals(
                        value[field],
                        f"{path}.{field}",
                        signals,
                        unknown_provenance,
                        trust=_OutcomeTrust.NESTED_RESULT,
                        depth=depth + 1,
                    )
                    break
        return

    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, item in enumerate(value):
            _collect_output_signals(
                item,
                f"{path}[{index}]",
                signals,
                unknown_provenance,
                trust=_OutcomeTrust.NESTED_RESULT,
                depth=depth + 1,
            )


def _resolve_outcome(
    signals: Sequence[_OutcomeSignal],
    unknown_provenance: Sequence[str],
) -> ToolOutcome:
    if not signals:
        provenance = unknown_provenance[0] if unknown_provenance else None
        return ToolOutcome(ToolOutcomeStatus.UNKNOWN, provenance=provenance)

    strongest_trust = min(signal.trust for signal in signals)
    strongest = [signal for signal in signals if signal.trust == strongest_trust]
    failed_signals = [signal for signal in strongest if not signal.succeeded]
    failed = next(
        (signal for signal in failed_signals if signal.exit_code is not None),
        failed_signals[0] if failed_signals else None,
    )
    succeeded_signals = [signal for signal in strongest if signal.succeeded]
    succeeded = next(
        (signal for signal in succeeded_signals if signal.exit_code is not None),
        succeeded_signals[0] if succeeded_signals else None,
    )
    if failed is not None and succeeded is not None:
        return ToolOutcome(
            ToolOutcomeStatus.UNKNOWN,
            provenance=f"conflicting_outcomes:{failed.provenance}|{succeeded.provenance}",
        )
    if failed is not None:
        return ToolOutcome(
            ToolOutcomeStatus.FAILED,
            exit_code=failed.exit_code,
            provenance=failed.provenance,
        )
    if succeeded is not None:
        return ToolOutcome(
            ToolOutcomeStatus.SUCCEEDED,
            exit_code=succeeded.exit_code,
            provenance=succeeded.provenance,
        )
    raise AssertionError("strongest outcome signals unexpectedly empty")


def normalize_tool_outcome(
    data: dict[str, Any],
    *,
    explicit_success: bool | None = None,
    provenance: str | None = None,
) -> ToolOutcome:
    """Derive and store a canonical outcome from structured result fields."""
    signals: list[_OutcomeSignal] = []
    unknown_provenance: list[str] = []

    existing = data.get("tool_outcome")
    if isinstance(existing, Mapping):
        if data.get("_tool_outcome_locked") is True:
            return tool_outcome_from_data(data)
        existing_trust = (
            _OutcomeTrust.PROVIDER_CONTRACT
            if data.get("_tool_outcome_trust") == "provider_contract"
            else _OutcomeTrust.DIRECT_RESULT
        )
        _collect_mapping_signals(
            existing,
            "tool_outcome",
            signals,
            unknown_provenance,
            existing_trust,
        )

    wrapper_tool = _normalized_tool_name(data) in _WRAPPER_TOOL_NAMES
    if wrapper_tool:
        wrapper_signals: list[_OutcomeSignal] = []
        _collect_mapping_signals(
            data,
            "event",
            wrapper_signals,
            unknown_provenance,
            _OutcomeTrust.DIRECT_RESULT,
        )
        signals.extend(signal for signal in wrapper_signals if not signal.succeeded)
    else:
        _collect_mapping_signals(
            data,
            "event",
            signals,
            unknown_provenance,
            _OutcomeTrust.DIRECT_RESULT,
        )

    output_field = next(
        (
            field
            for field in ("tool_output", "tool_result", "tool_response", "contentItems")
            if field in data
        ),
        None,
    )
    if output_field is not None:
        _collect_output_signals(
            data[output_field],
            output_field,
            signals,
            unknown_provenance,
            trust=(_OutcomeTrust.NESTED_RESULT if wrapper_tool else _OutcomeTrust.DIRECT_RESULT),
        )

    if explicit_success is not None:
        matching_exit = next(
            (
                signal.exit_code
                for signal in signals
                if signal.succeeded is explicit_success and signal.exit_code is not None
            ),
            None,
        )
        explicit_provenance = provenance or "adapter.explicit_tool_outcome"
        signals.append(
            _OutcomeSignal(
                explicit_success,
                explicit_provenance,
                _OutcomeTrust.PROVIDER_CONTRACT,
                matching_exit,
            )
        )
        data["_tool_outcome_trust"] = "provider_contract"
    outcome = _resolve_outcome(signals, unknown_provenance)
    data["tool_outcome"] = outcome.to_dict()
    return outcome


def tool_outcome_from_data(data: Mapping[str, Any] | None) -> ToolOutcome:
    """Read a normalized outcome, preserving fail-closed behavior."""
    if not isinstance(data, Mapping):
        return ToolOutcome(ToolOutcomeStatus.UNKNOWN)
    value = data.get("tool_outcome")
    if not isinstance(value, Mapping):
        return ToolOutcome(ToolOutcomeStatus.UNKNOWN)
    raw_status = value.get("status")
    if not isinstance(raw_status, str):
        return ToolOutcome(ToolOutcomeStatus.UNKNOWN)
    try:
        status = ToolOutcomeStatus(raw_status)
    except (TypeError, ValueError):
        return ToolOutcome(ToolOutcomeStatus.UNKNOWN)
    exit_code = value.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = None
    provenance = value.get("provenance")
    if not isinstance(provenance, str):
        provenance = None
    return ToolOutcome(status, exit_code=exit_code, provenance=provenance)


__all__ = [
    "ToolOutcome",
    "ToolOutcomeStatus",
    "normalize_tool_outcome",
    "tool_outcome_from_data",
]
