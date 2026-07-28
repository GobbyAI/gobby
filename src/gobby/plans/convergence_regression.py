"""Persisted-telemetry reader for the live plan convergence regression."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_telemetry import (
    derive_convergence_comparison,
    validate_convergence_telemetry,
)

PLAN_COMMAND = "/gobby plan"
_CANONICAL_LANES = {"requirements", "failure-paths", "integration"}

PlanRunner = Callable[[str, Path], None]
PersistedRoundLoader = Callable[[Path], Sequence[Mapping[str, object]]]


def build_convergence_comparison(
    *,
    plan_path: Path,
    baseline_rounds_to_approval: int,
    persisted_rounds: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build the comparison artifact from durable round results."""
    if baseline_rounds_to_approval < 1:
        raise ValueError("baseline_rounds_to_approval must be positive")
    if not persisted_rounds:
        raise ReviewEvidenceError(
            "missing_convergence_telemetry",
            "live regression produced no persisted rounds",
        )

    rounds = sorted(persisted_rounds, key=_round_number)
    expected_rounds = list(range(1, len(rounds) + 1))
    actual_rounds = [_round_number(round) for round in rounds]
    if actual_rounds != expected_rounds:
        raise ReviewEvidenceError(
            "invalid_convergence_telemetry",
            "persisted rounds must be unique and contiguous from round 1",
        )

    telemetry_records: list[dict[str, object]] = []
    wall_times: list[float] = []
    finding_tail: list[int] = []
    classes_by_round: list[list[str]] = []
    lane_ids_by_round: list[list[str]] = []
    approval_round: int | None = None

    for round_result in rounds:
        telemetry = validate_convergence_telemetry(
            _mapping(round_result.get("convergence_telemetry"), "convergence_telemetry"),
            required_state="enriched",
        )
        telemetry_records.append(telemetry)
        reviewer = _mapping(telemetry.get("reviewer"), "reviewer")
        daemon = _mapping(telemetry.get("daemon"), "daemon")
        wall_times.append(_number(daemon.get("wall_time_seconds"), "wall_time_seconds"))
        finding_tail.append(len(_list(round_result.get("findings"), "findings")))
        classes_by_round.append(_round_check_key_classes(reviewer))

        lanes = [
            cast(str, lane["lane_id"]) for lane in _object_list(daemon.get("lanes"), "daemon.lanes")
        ]
        if set(lanes) != _CANONICAL_LANES:
            raise ReviewEvidenceError(
                "invalid_convergence_telemetry",
                "each persisted round must retain exhaustive canonical lane coverage",
            )
        lane_ids_by_round.append(lanes)

        verdict = round_result.get("verdict")
        if verdict == "approved" and approval_round is None:
            approval_round = _round_number(round_result)
        elif verdict not in {"needs_review", "approved"}:
            raise ReviewEvidenceError(
                "invalid_convergence_telemetry",
                "persisted round verdict must be needs_review or approved",
            )

    if approval_round is None:
        raise ReviewEvidenceError(
            "convergence_regression",
            "live regression did not reach approval",
        )
    if approval_round != len(rounds):
        raise ReviewEvidenceError(
            "convergence_regression",
            "persisted rounds continue after approval",
        )

    comparison = derive_convergence_comparison(telemetry_records)
    current: dict[str, object] = {
        "rounds_to_approval": approval_round,
        "fixer_induced_count": comparison["fixer_induced_count"],
        "repeated_check_keys": comparison["repeated_check_keys"],
        "check_key_classes_by_round": classes_by_round,
        "per_round_wall_time_seconds": wall_times,
        "ledger_entries_carried": comparison["ledger_entries_carried"],
        "finding_tail": finding_tail,
        "lane_ids_by_round": lane_ids_by_round,
    }
    return {
        "command": PLAN_COMMAND,
        "plan_path": plan_path.as_posix(),
        "baseline": {"rounds_to_approval": baseline_rounds_to_approval},
        "current": current,
    }


def assert_convergence_targets(artifact: Mapping[str, object]) -> None:
    """Assert convergence targets independently from wall-time variance."""
    current = _mapping(artifact.get("current"), "current")
    rounds_to_approval = _integer(current.get("rounds_to_approval"), "rounds_to_approval")
    if rounds_to_approval >= 10:
        raise AssertionError(
            f"rounds-to-approval regression: expected single digits, got {rounds_to_approval}"
        )

    repeated_keys = _string_list(current.get("repeated_check_keys"), "repeated_check_keys")
    if repeated_keys:
        raise AssertionError(f"exact check-key repeat regression: {repeated_keys}")

    class_rounds = [
        set(_string_list(raw, "check_key_classes_by_round"))
        for raw in _list(current.get("check_key_classes_by_round"), "check_key_classes_by_round")
    ]
    for previous, current_classes in zip(class_rounds, class_rounds[1:], strict=False):
        overlap = previous & current_classes
        if overlap:
            raise AssertionError(f"consecutive rounds share check-key class: {sorted(overlap)}")

    finding_tail = [
        _integer(value, "finding_tail")
        for value in _list(current.get("finding_tail"), "finding_tail")
    ]
    if len(finding_tail) < 2 or any(
        previous <= following
        for previous, following in zip(finding_tail, finding_tail[1:], strict=False)
    ):
        raise AssertionError(f"non-decaying finding tail regression: {finding_tail}")


def assert_wall_time_variance(
    artifact: Mapping[str, object],
    *,
    maximum_ratio: float,
) -> None:
    """Bound timing variance separately from semantic convergence."""
    if maximum_ratio < 1:
        raise ValueError("maximum_ratio must be at least 1")
    wall_times = [
        _number(value, "per_round_wall_time_seconds")
        for value in _list(
            _mapping(artifact.get("current"), "current").get("per_round_wall_time_seconds"),
            "per_round_wall_time_seconds",
        )
    ]
    if not wall_times:
        raise AssertionError("wall-time variance requires at least one persisted round")
    minimum = min(wall_times)
    maximum = max(wall_times)
    if (minimum == 0 and maximum > 0) or (minimum > 0 and maximum / minimum > maximum_ratio):
        raise AssertionError(
            f"wall-time variance regression: min={minimum}, max={maximum}, "
            f"maximum_ratio={maximum_ratio}"
        )


def run_live_convergence_regression(
    *,
    plan_path: Path,
    artifact_path: Path,
    baseline_rounds_to_approval: int,
    run_plan: PlanRunner,
    load_persisted_rounds: PersistedRoundLoader,
) -> dict[str, object]:
    """Run `/gobby plan`, read its persisted rounds, and write the comparison."""
    run_plan(PLAN_COMMAND, plan_path)
    artifact = build_convergence_comparison(
        plan_path=plan_path,
        baseline_rounds_to_approval=baseline_rounds_to_approval,
        persisted_rounds=load_persisted_rounds(plan_path),
    )
    artifact_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert_convergence_targets(artifact)
    return artifact


def _round_check_key_classes(reviewer: Mapping[str, object]) -> list[str]:
    classes: list[str] = []
    for field in ("reviewer_miss", "fixer_induced", "repeated_check_keys"):
        group = _mapping(reviewer.get(field), field)
        for classification in _object_list(group.get("classifications"), field):
            check_key_class = classification.get("check_key_class")
            if isinstance(check_key_class, str) and check_key_class not in classes:
                classes.append(check_key_class)
    return classes


def _round_number(round_result: Mapping[str, object]) -> int:
    value = _integer(round_result.get("round_number"), "round_number")
    if value < 1:
        raise ReviewEvidenceError(
            "invalid_convergence_telemetry",
            "round_number must be positive",
        )
    return value


def _mapping(raw: object, field: str) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ReviewEvidenceError(
            "invalid_convergence_telemetry",
            f"{field} must be an object",
        )
    return dict(raw)


def _list(raw: object, field: str) -> list[object]:
    if not isinstance(raw, list):
        raise ReviewEvidenceError(
            "invalid_convergence_telemetry",
            f"{field} must be a list",
        )
    return raw


def _object_list(raw: object, field: str) -> list[dict[str, object]]:
    values = _list(raw, field)
    if not all(isinstance(value, Mapping) for value in values):
        raise ReviewEvidenceError(
            "invalid_convergence_telemetry",
            f"{field} entries must be objects",
        )
    return [dict(cast(Mapping[str, object], value)) for value in values]


def _string_list(raw: object, field: str) -> list[str]:
    values = _list(raw, field)
    if not all(isinstance(value, str) for value in values):
        raise ReviewEvidenceError(
            "invalid_convergence_telemetry",
            f"{field} entries must be strings",
        )
    return cast(list[str], values)


def _integer(raw: object, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ReviewEvidenceError(
            "invalid_convergence_telemetry",
            f"{field} must be an integer",
        )
    return raw


def _number(raw: object, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ReviewEvidenceError(
            "invalid_convergence_telemetry",
            f"{field} must be a number",
        )
    value = float(raw)
    if value < 0:
        raise ReviewEvidenceError(
            "invalid_convergence_telemetry",
            f"{field} must be non-negative",
        )
    return value
