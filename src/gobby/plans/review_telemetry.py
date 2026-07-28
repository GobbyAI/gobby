"""Strict convergence telemetry carried by canonical plan-review results."""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from gobby.plans.review_evidence_models import ReviewEvidenceError

_NONEMPTY_STRING = {"type": "string", "minLength": 1}
_STRING_ARRAY = {
    "type": "array",
    "items": _NONEMPTY_STRING,
    "uniqueItems": True,
}
_CLASSIFICATION_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": _NONEMPTY_STRING,
        "value": _NONEMPTY_STRING,
    },
    "required": ["name", "value"],
    "additionalProperties": False,
}
_PROVENANCE_PROPERTIES = {
    "finding_ids": _STRING_ARRAY,
    "ledger_ids": _STRING_ARRAY,
    "classification_inputs": {
        "type": "array",
        "items": _CLASSIFICATION_INPUT_SCHEMA,
        "minItems": 1,
    },
}
_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "check_key": _NONEMPTY_STRING,
        "check_key_class": _NONEMPTY_STRING,
        **_PROVENANCE_PROPERTIES,
    },
    "required": [
        "check_key",
        "check_key_class",
        "finding_ids",
        "ledger_ids",
        "classification_inputs",
    ],
    "anyOf": [
        {"properties": {"finding_ids": {"minItems": 1}}},
        {"properties": {"ledger_ids": {"minItems": 1}}},
    ],
    "additionalProperties": False,
}
_CLASSIFICATION_SET_SCHEMA = {
    "type": "object",
    "properties": {
        "count": {"type": "integer", "minimum": 0},
        "classifications": {
            "type": "array",
            "items": _CLASSIFICATION_SCHEMA,
        },
    },
    "required": ["count", "classifications"],
    "additionalProperties": False,
}
_PROVENANCE_REQUIRED = ["finding_ids", "ledger_ids", "classification_inputs"]
_REVIEWER_AVAILABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"const": "available"},
        "reviewer_miss": _CLASSIFICATION_SET_SCHEMA,
        "fixer_induced": _CLASSIFICATION_SET_SCHEMA,
        "repeated_check_keys": _CLASSIFICATION_SET_SCHEMA,
        "remedy_scope": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["none", "local", "cross_section", "new_deliverable"],
                },
                **_PROVENANCE_PROPERTIES,
            },
            "required": ["scope", *_PROVENANCE_REQUIRED],
            "additionalProperties": False,
        },
        "ledger_entries_carried": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 0},
                **_PROVENANCE_PROPERTIES,
            },
            "required": ["count", *_PROVENANCE_REQUIRED],
            "additionalProperties": False,
        },
        "artifact_growth": {
            "type": "object",
            "properties": {
                "section_delta": {"type": "integer"},
                "target_delta": {"type": "integer"},
                "acceptance_delta": {"type": "integer"},
                **_PROVENANCE_PROPERTIES,
            },
            "required": [
                "section_delta",
                "target_delta",
                "acceptance_delta",
                *_PROVENANCE_REQUIRED,
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        "status",
        "reviewer_miss",
        "fixer_induced",
        "repeated_check_keys",
        "remedy_scope",
        "ledger_entries_carried",
        "artifact_growth",
    ],
    "additionalProperties": False,
}
_REVIEWER_UNAVAILABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"const": "unavailable"},
        "reason": {
            "type": "string",
            "enum": ["reviewer_result_not_delivered"],
        },
    },
    "required": ["status", "reason"],
    "additionalProperties": False,
}
_AVAILABLE_NUMBER_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "number", "minimum": 0},
    },
    "required": ["value"],
    "additionalProperties": False,
}
_UNAVAILABLE_NUMBER_SCHEMA = {
    "type": "object",
    "properties": {
        "unavailable": {
            "type": "string",
            "enum": ["native_lane_events_unavailable", "no_findings"],
        },
    },
    "required": ["unavailable"],
    "additionalProperties": False,
}
_MEASURED_NUMBER_SCHEMA = {
    "oneOf": [_AVAILABLE_NUMBER_SCHEMA, _UNAVAILABLE_NUMBER_SCHEMA],
}
_LANE_AGGREGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "lane_id": {
            "type": "string",
            "enum": ["requirements", "failure-paths", "integration"],
        },
        "duration_seconds": _MEASURED_NUMBER_SCHEMA,
        "tool_calls": _MEASURED_NUMBER_SCHEMA,
    },
    "required": ["lane_id", "duration_seconds", "tool_calls"],
    "additionalProperties": False,
}
_DAEMON_AGGREGATES_SCHEMA = {
    "type": "object",
    "properties": {
        "terminal_status": {
            "type": "string",
            "enum": ["success", "error", "timeout", "cancelled"],
        },
        "wall_time_seconds": {"type": "number", "minimum": 0},
        "tool_calls": {"type": "integer", "minimum": 0},
        "turns": {"type": "integer", "minimum": 0},
        "calls_per_finding": _MEASURED_NUMBER_SCHEMA,
        "lanes": {
            "type": "array",
            "items": _LANE_AGGREGATE_SCHEMA,
            "minItems": 3,
            "maxItems": 3,
        },
    },
    "required": [
        "terminal_status",
        "wall_time_seconds",
        "tool_calls",
        "turns",
        "calls_per_finding",
        "lanes",
    ],
    "additionalProperties": False,
}

CONVERGENCE_TELEMETRY_SCHEMA: dict[str, object] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "state": {"const": "delivered"},
                "reviewer": _REVIEWER_AVAILABLE_SCHEMA,
            },
            "required": ["state", "reviewer"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "state": {"const": "enriched"},
                "reviewer": {
                    "oneOf": [
                        _REVIEWER_AVAILABLE_SCHEMA,
                        _REVIEWER_UNAVAILABLE_SCHEMA,
                    ]
                },
                "daemon": _DAEMON_AGGREGATES_SCHEMA,
            },
            "required": ["state", "reviewer", "daemon"],
            "additionalProperties": False,
        },
    ]
}

_REVIEW_MESSAGE_NAMESPACE = uuid.UUID("edbe2f92-21a1-4b6e-8b54-d2cc47a0de71")
_AVAILABLE_REVIEWER_KEYS = {
    "status",
    "reviewer_miss",
    "fixer_induced",
    "repeated_check_keys",
    "remedy_scope",
    "ledger_entries_carried",
    "artifact_growth",
}
_PROVENANCE_KEYS = {"finding_ids", "ledger_ids", "classification_inputs"}


def validate_convergence_telemetry(
    raw: Mapping[str, object],
    *,
    required_state: str | None = None,
) -> dict[str, object]:
    """Validate telemetry semantics and return a detached JSON object."""
    payload = _canonical_object(raw)
    state = payload.get("state")
    if state not in {"delivered", "enriched"}:
        raise _invalid("state must be delivered or enriched")
    if required_state is not None and state != required_state:
        raise _invalid(f"state must be {required_state}")
    expected_keys = (
        {"state", "reviewer"} if state == "delivered" else {"state", "reviewer", "daemon"}
    )
    if set(payload) != expected_keys:
        raise _invalid(f"{state} telemetry must contain exactly {sorted(expected_keys)}")

    reviewer = _mapping(payload.get("reviewer"), "reviewer")
    reviewer_status = reviewer.get("status")
    if reviewer_status == "unavailable":
        if state != "enriched" or reviewer != {
            "status": "unavailable",
            "reason": "reviewer_result_not_delivered",
        }:
            raise _invalid("reviewer unavailable is valid only for enriched undelivered results")
    elif reviewer_status == "available":
        _validate_available_reviewer(reviewer)
    else:
        raise _invalid("reviewer.status must be available or unavailable")

    if state == "enriched":
        _validate_daemon_aggregates(_mapping(payload.get("daemon"), "daemon"))
    return payload


def derive_convergence_comparison(
    telemetry_records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Derive the E1 comparison fields from persisted telemetry alone."""
    misses = 0
    fixer_induced = 0
    repeated_keys: list[str] = []
    repeated_classes: list[str] = []
    ledger_entries = 0
    artifact_growth = {
        "section_delta": 0,
        "target_delta": 0,
        "acceptance_delta": 0,
    }
    for raw in telemetry_records:
        telemetry = validate_convergence_telemetry(raw)
        reviewer = cast(dict[str, object], telemetry["reviewer"])
        if reviewer["status"] != "available":
            continue
        misses += _count(reviewer["reviewer_miss"], "reviewer_miss")
        fixer_induced += _count(reviewer["fixer_induced"], "fixer_induced")
        repeated = _mapping(reviewer["repeated_check_keys"], "repeated_check_keys")
        for item in _object_list(repeated["classifications"], "classifications"):
            repeated_keys.append(cast(str, item["check_key"]))
            repeated_classes.append(cast(str, item["check_key_class"]))
        ledger_entries += _count(reviewer["ledger_entries_carried"], "ledger_entries_carried")
        growth = _mapping(reviewer["artifact_growth"], "artifact_growth")
        for field in artifact_growth:
            artifact_growth[field] += _integer(growth.get(field), f"artifact_growth.{field}")
    return {
        "reviewer_miss_count": misses,
        "fixer_induced_count": fixer_induced,
        "repeated_check_keys": repeated_keys,
        "repeated_check_key_classes": repeated_classes,
        "ledger_entries_carried": ledger_entries,
        "artifact_growth": artifact_growth,
    }


def derive_daemon_aggregates(
    run: object,
    *,
    terminal_status: str,
    finding_count: int,
    completed_at: datetime | None = None,
) -> dict[str, object]:
    """Derive post-run counters from the authoritative bound AgentRun."""
    if terminal_status not in {"success", "error", "timeout", "cancelled"}:
        raise _invalid("terminal_status is invalid")
    if finding_count < 0:
        raise _invalid("finding_count must be non-negative")
    started_at = getattr(run, "started_at", None) or getattr(run, "created_at", None)
    settled_at = completed_at or getattr(run, "completed_at", None)
    if not isinstance(started_at, datetime) or not isinstance(settled_at, datetime):
        raise _invalid("run requires started/created and completed timestamps")
    wall_time_seconds = max(0.0, (settled_at - started_at).total_seconds())
    tool_calls = _nonnegative_integer(
        getattr(run, "tool_calls_count", None),
        "run.tool_calls_count",
    )
    turns = _nonnegative_integer(getattr(run, "turns_used", None), "run.turns_used")
    calls_per_finding: dict[str, object]
    if finding_count == 0:
        calls_per_finding = {"unavailable": "no_findings"}
    else:
        calls_per_finding = {"value": tool_calls / finding_count}
    unavailable = {"unavailable": "native_lane_events_unavailable"}
    lanes = [
        {
            "lane_id": lane_id,
            "duration_seconds": dict(unavailable),
            "tool_calls": dict(unavailable),
        }
        for lane_id in ("requirements", "failure-paths", "integration")
    ]
    return {
        "terminal_status": terminal_status,
        "wall_time_seconds": wall_time_seconds,
        "tool_calls": tool_calls,
        "turns": turns,
        "calls_per_finding": calls_per_finding,
        "lanes": lanes,
    }


def deterministic_review_message_id(
    *,
    evidence_id: str,
    run_id: str,
    effect_kind: str,
    target_session_id: str,
) -> str:
    """Derive one durable message identity for a replayable review effect."""
    parts = (evidence_id, run_id, effect_kind, target_session_id)
    if any(not part for part in parts):
        raise ValueError("review message identity parts must be non-empty strings")
    return str(uuid.uuid5(_REVIEW_MESSAGE_NAMESPACE, "\x1f".join(parts)))


def persist_delivered_round_result(
    db: Any,
    *,
    run_id: str,
    round_result: Mapping[str, object],
) -> dict[str, object]:
    """CAS a validated delivered result without allowing state regression."""
    from gobby.plans.review_evidence_models import validate_round_result

    payload = validate_round_result(round_result)
    telemetry = _mapping(payload.get("convergence_telemetry"), "convergence_telemetry")
    validate_convergence_telemetry(telemetry, required_state="delivered")
    return _persist_result_state(
        db,
        run_id=run_id,
        payload=payload,
        expected_current_state=None,
    )


def persist_enriched_round_result(
    db: Any,
    *,
    run_id: str,
    round_result: Mapping[str, object],
) -> dict[str, object]:
    """CAS delivered telemetry to enriched while preserving reviewer content."""
    from gobby.plans.review_evidence_models import validate_round_result

    payload = validate_round_result(round_result)
    telemetry = _mapping(payload.get("convergence_telemetry"), "convergence_telemetry")
    validate_convergence_telemetry(telemetry, required_state="enriched")
    return _persist_result_state(
        db,
        run_id=run_id,
        payload=payload,
        expected_current_state="delivered",
    )


def enrich_round_result(
    raw: Mapping[str, object],
    *,
    run: object,
    terminal_status: str,
    completed_at: datetime | None = None,
) -> dict[str, object]:
    """Idempotently merge daemon-owned aggregates into a delivered result."""
    result = _canonical_object(raw)
    telemetry = _mapping(result.get("convergence_telemetry"), "convergence_telemetry")
    findings = result.get("findings")
    finding_count = len(findings) if isinstance(findings, list) else 0
    daemon = derive_daemon_aggregates(
        run,
        terminal_status=terminal_status,
        finding_count=finding_count,
        completed_at=completed_at,
    )
    state = telemetry.get("state")
    if state == "enriched":
        validated = validate_convergence_telemetry(telemetry, required_state="enriched")
        if validated.get("daemon") != daemon:
            raise _invalid("enriched daemon aggregates conflict with the bound run")
        return result
    delivered = validate_convergence_telemetry(telemetry, required_state="delivered")
    result["convergence_telemetry"] = {
        "state": "enriched",
        "reviewer": delivered["reviewer"],
        "daemon": daemon,
    }
    return result


async def settle_review_result_before_wake(
    raw: Mapping[str, object],
    *,
    run: object,
    terminal_status: str,
    persist: Callable[
        [dict[str, object]],
        Mapping[str, object] | Awaitable[Mapping[str, object]],
    ],
    wake: Callable[[], object | Awaitable[object]],
    completed_at: datetime | None = None,
) -> dict[str, object]:
    """Persist an enriched result before issuing the best-effort parent wake."""
    enriched = enrich_round_result(
        raw,
        run=run,
        terminal_status=terminal_status,
        completed_at=completed_at,
    )
    persisted = persist(enriched)
    if inspect.isawaitable(persisted):
        persisted = await persisted
    durable = _canonical_object(persisted)
    validate_convergence_telemetry(
        _mapping(durable.get("convergence_telemetry"), "convergence_telemetry"),
        required_state="enriched",
    )
    wake_result = wake()
    if inspect.isawaitable(wake_result):
        await wake_result
    return durable


def _persist_result_state(
    db: Any,
    *,
    run_id: str,
    payload: dict[str, object],
    expected_current_state: str | None,
) -> dict[str, object]:
    payload_evidence_id = _round_result_evidence_id(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with db.transaction() as transaction:
        row = transaction.execute(
            """
            SELECT ar.result, ar.status, pre.evidence_id,
                   pre.finalized_at, pre.expired_at
              FROM agent_runs ar
              JOIN plan_review_evidence pre ON pre.dispatch_run_id = ar.id
             WHERE ar.id = %s
             FOR UPDATE OF ar, pre
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise ReviewEvidenceError(
                "review_result_unbound",
                f"run {run_id} has no bound plan-review evidence",
            )
        if row["evidence_id"] != payload_evidence_id:
            raise ReviewEvidenceError(
                "coverage_evidence_mismatch",
                "round result belongs to a different review evidence snapshot",
            )
        if row["finalized_at"] is not None or row["expired_at"] is not None:
            raise ReviewEvidenceError(
                "review_result_terminal",
                "cannot write a result for finalized or expired evidence",
            )

        current_raw = row["result"]
        if current_raw is not None:
            try:
                current_value = json.loads(str(current_raw))
            except json.JSONDecodeError as exc:
                raise ReviewEvidenceError(
                    "review_result_conflict",
                    "stored run result is not canonical review JSON",
                ) from exc
            if current_value == payload:
                return payload
            if not isinstance(current_value, dict):
                raise ReviewEvidenceError(
                    "review_result_conflict",
                    "stored run result is not a review object",
                )
            current = cast(dict[str, object], current_value)
            current_telemetry = _mapping(
                current.get("convergence_telemetry"),
                "stored convergence_telemetry",
            )
            current_state = current_telemetry.get("state")
            if expected_current_state is None:
                if current_state == "enriched":
                    raise ReviewEvidenceError(
                        "review_result_regression",
                        "delivered result cannot regress an enriched result",
                    )
                raise ReviewEvidenceError(
                    "review_result_conflict",
                    "delivered result conflicts with the stored result",
                )
            if current_state != expected_current_state:
                raise ReviewEvidenceError(
                    "review_result_conflict",
                    f"enrichment requires stored state {expected_current_state}",
                )
            incoming_telemetry = _mapping(
                payload["convergence_telemetry"],
                "convergence_telemetry",
            )
            current_without_telemetry = {
                key: value for key, value in current.items() if key != "convergence_telemetry"
            }
            payload_without_telemetry = {
                key: value for key, value in payload.items() if key != "convergence_telemetry"
            }
            if current_without_telemetry != payload_without_telemetry or current_telemetry.get(
                "reviewer"
            ) != incoming_telemetry.get("reviewer"):
                raise ReviewEvidenceError(
                    "review_result_conflict",
                    "enriched result changes reviewer-owned content",
                )
        elif expected_current_state is not None:
            raise ReviewEvidenceError(
                "review_result_conflict",
                "enrichment requires a durable delivered result",
            )

        if row["status"] not in {"pending", "running"}:
            raise ReviewEvidenceError(
                "review_result_terminal",
                "cannot change result after the run is terminal",
            )
        updated = transaction.execute(
            """
            UPDATE agent_runs
               SET result = %s, updated_at = NOW()
             WHERE id = %s AND status IN ('pending', 'running')
            RETURNING id
            """,
            (encoded, run_id),
        ).fetchone()
        if updated is None:
            raise ReviewEvidenceError(
                "review_result_cas_failed",
                "run state changed while persisting review result",
            )
    return payload


def _round_result_evidence_id(payload: Mapping[str, object]) -> str:
    verdict = payload.get("verdict")
    if verdict in {"approved", "needs_review"}:
        coverage = _mapping(payload.get("coverage_attestation"), "coverage_attestation")
        evidence_id = coverage.get("evidence_id")
    else:
        evidence_id = payload.get("evidence_id")
    return _nonempty_string(evidence_id, "round_result.evidence_id")


def _validate_available_reviewer(reviewer: Mapping[str, object]) -> None:
    if set(reviewer) != _AVAILABLE_REVIEWER_KEYS:
        raise _invalid("available reviewer telemetry has an invalid field set")
    for field in ("reviewer_miss", "fixer_induced", "repeated_check_keys"):
        group = _mapping(reviewer.get(field), field)
        if set(group) != {"count", "classifications"}:
            raise _invalid(f"{field} must contain count and classifications")
        classifications = _object_list(group.get("classifications"), f"{field}.classifications")
        count = _integer(group.get("count"), f"{field}.count")
        if count < 0 or count != len(classifications):
            raise _invalid(f"{field}.count must match classifications length")
        for classification in classifications:
            if set(classification) != {
                "check_key",
                "check_key_class",
                *_PROVENANCE_KEYS,
            }:
                raise _invalid(f"{field} classification has an invalid field set")
            _nonempty_string(classification.get("check_key"), f"{field}.check_key")
            _nonempty_string(
                classification.get("check_key_class"),
                f"{field}.check_key_class",
            )
            _validate_provenance(classification, field=field, require_contributor=True)

    remedy_scope = _mapping(reviewer.get("remedy_scope"), "remedy_scope")
    if set(remedy_scope) != {"scope", *_PROVENANCE_KEYS}:
        raise _invalid("remedy_scope has an invalid field set")
    if remedy_scope.get("scope") not in {"none", "local", "cross_section", "new_deliverable"}:
        raise _invalid("remedy_scope.scope is invalid")
    _validate_provenance(
        remedy_scope,
        field="remedy_scope",
        require_contributor=remedy_scope.get("scope") != "none",
    )

    ledger = _mapping(reviewer.get("ledger_entries_carried"), "ledger_entries_carried")
    if set(ledger) != {"count", *_PROVENANCE_KEYS}:
        raise _invalid("ledger_entries_carried has an invalid field set")
    ledger_count = _integer(ledger.get("count"), "ledger_entries_carried.count")
    ledger_ids = _string_list(ledger.get("ledger_ids"), "ledger_entries_carried.ledger_ids")
    if ledger_count < 0 or ledger_count != len(ledger_ids):
        raise _invalid("ledger_entries_carried.count must match ledger_ids length")
    _validate_provenance(
        ledger,
        field="ledger_entries_carried",
        require_contributor=ledger_count > 0,
    )

    growth = _mapping(reviewer.get("artifact_growth"), "artifact_growth")
    if set(growth) != {
        "section_delta",
        "target_delta",
        "acceptance_delta",
        *_PROVENANCE_KEYS,
    }:
        raise _invalid("artifact_growth has an invalid field set")
    deltas = [
        _integer(growth.get(field), f"artifact_growth.{field}")
        for field in ("section_delta", "target_delta", "acceptance_delta")
    ]
    _validate_provenance(
        growth,
        field="artifact_growth",
        require_contributor=any(delta != 0 for delta in deltas),
    )


def _validate_provenance(
    raw: Mapping[str, object],
    *,
    field: str,
    require_contributor: bool,
) -> None:
    finding_ids = _string_list(raw.get("finding_ids"), f"{field}.finding_ids")
    ledger_ids = _string_list(raw.get("ledger_ids"), f"{field}.ledger_ids")
    inputs = _object_list(raw.get("classification_inputs"), f"{field}.classification_inputs")
    if require_contributor and not finding_ids and not ledger_ids:
        raise _invalid(f"{field} requires a contributing finding or ledger ID")
    if not inputs:
        raise _invalid(f"{field}.classification_inputs must be non-empty")
    for item in inputs:
        if set(item) != {"name", "value"}:
            raise _invalid(f"{field}.classification_inputs entries require name and value")
        _nonempty_string(item.get("name"), f"{field}.classification_inputs.name")
        _nonempty_string(item.get("value"), f"{field}.classification_inputs.value")


def _validate_daemon_aggregates(raw: Mapping[str, object]) -> None:
    expected = {
        "terminal_status",
        "wall_time_seconds",
        "tool_calls",
        "turns",
        "calls_per_finding",
        "lanes",
    }
    if set(raw) != expected:
        raise _invalid("daemon aggregates have an invalid field set")
    if raw.get("terminal_status") not in {"success", "error", "timeout", "cancelled"}:
        raise _invalid("daemon.terminal_status is invalid")
    _nonnegative_number(raw.get("wall_time_seconds"), "daemon.wall_time_seconds")
    for field in ("tool_calls", "turns"):
        if _integer(raw.get(field), f"daemon.{field}") < 0:
            raise _invalid(f"daemon.{field} must be non-negative")
    _validate_measurement(raw.get("calls_per_finding"), "daemon.calls_per_finding")
    lanes = _object_list(raw.get("lanes"), "daemon.lanes")
    if len(lanes) != 3 or {lane.get("lane_id") for lane in lanes} != {
        "requirements",
        "failure-paths",
        "integration",
    }:
        raise _invalid("daemon.lanes must contain each canonical lane exactly once")
    for lane in lanes:
        if set(lane) != {"lane_id", "duration_seconds", "tool_calls"}:
            raise _invalid("daemon lane has an invalid field set")
        _validate_measurement(lane.get("duration_seconds"), "daemon.lane.duration_seconds")
        _validate_measurement(lane.get("tool_calls"), "daemon.lane.tool_calls")


def _validate_measurement(raw: object, field: str) -> None:
    value = _mapping(raw, field)
    if set(value) == {"value"}:
        _nonnegative_number(value.get("value"), f"{field}.value")
        return
    if set(value) == {"unavailable"} and value.get("unavailable") in {
        "native_lane_events_unavailable",
        "no_findings",
    }:
        return
    raise _invalid(f"{field} must carry value or an explicit unavailable reason")


def _canonical_object(raw: Mapping[str, object]) -> dict[str, object]:
    try:
        value = json.loads(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise _invalid(f"payload must be JSON-serializable: {exc}") from exc
    if not isinstance(value, dict):
        raise _invalid("payload must be an object")
    return cast(dict[str, object], value)


def _mapping(raw: object, field: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise _invalid(f"{field} must be an object")
    return cast(dict[str, object], raw)


def _object_list(raw: object, field: str) -> list[dict[str, object]]:
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise _invalid(f"{field} must be an object array")
    return cast(list[dict[str, object]], raw)


def _string_list(raw: object, field: str) -> list[str]:
    if (
        not isinstance(raw, list)
        or any(not isinstance(item, str) or not item for item in raw)
        or len(raw) != len(set(raw))
    ):
        raise _invalid(f"{field} must be a unique string array")
    return cast(list[str], raw)


def _count(raw: object, field: str) -> int:
    return _integer(_mapping(raw, field).get("count"), f"{field}.count")


def _integer(raw: object, field: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise _invalid(f"{field} must be an integer")
    return raw


def _nonnegative_integer(raw: object, field: str) -> int:
    value = _integer(raw, field)
    if value < 0:
        raise _invalid(f"{field} must be non-negative")
    return value


def _nonnegative_number(raw: object, field: str) -> float:
    if not isinstance(raw, int | float) or isinstance(raw, bool) or raw < 0:
        raise _invalid(f"{field} must be a non-negative number")
    return float(raw)


def _nonempty_string(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise _invalid(f"{field} must be a non-empty string")
    return raw


def _invalid(message: str) -> ReviewEvidenceError:
    return ReviewEvidenceError("invalid_convergence_telemetry", message)
