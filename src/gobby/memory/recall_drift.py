"""Recall-quality drift detection with a regression alarm.

#17201 (epic #17099 Phase 3b) — the guardrail against silent degradation.
The monitor recomputes the SAME IPS-weighted pairwise ordering accuracy the
#17198 ship gate scored its holdout with (:mod:`gobby.memory.recall_fit`),
over a rolling window of recent live labeled recall signals, replayed under
the currently-effective constants (#17200,
:mod:`gobby.memory.recall_constants`). It compares that live accuracy against
the holdout accuracy recorded in the gate decision record for the active
regime and raises an alarm when the live value falls more than a configured
threshold below the baseline.

Two floors keep the alarm honest:

1. live floor — a window with fewer labeled pairs than ``min_pairs`` cannot
   alarm; a starved window is noise, not drift.
2. baseline floor — a decision record whose holdout had fewer pairs than
   ``min_pairs`` provides no baseline (the current data-starved reject record
   reports ``no_baseline`` rather than comparing against vapor).

Response path (documented in every report and alarm log line): when fitted
constants are active, the one-flag rollback is
``memory.use_fitted_recall_constants=false`` — static constants are the
permanent floor (#17200); re-enable only after rerunning the #17198 ship gate
(``run_ship_gate_from_store``). When static constants are active there is no
flag to flip; drift means the signal pipeline or environment changed —
investigate and rerun the ship gate to re-baseline.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from gobby.memory.recall_constants import (
    RecallConstants,
    _load_decision_record,
    decision_record_path,
    resolve_recall_constants,
)
from gobby.memory.recall_fit import (
    ReplayParams,
    ReplayRow,
    estimate_position_propensities,
    evaluate_pairwise,
    replay_row_from_signal_row,
)
from gobby.memory.recall_refit import MIN_EVAL_PAIRS
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.storage.recall_signals import RecallSignalStore

logger = logging.getLogger(__name__)

DEFAULT_ACCURACY_DROP = 0.05


@dataclass(frozen=True)
class DriftThresholds:
    """Alarm sensitivity: accuracy delta plus the pair floors."""

    accuracy_drop: float = DEFAULT_ACCURACY_DROP
    min_pairs: int = MIN_EVAL_PAIRS


@dataclass(frozen=True)
class DriftReport:
    """One drift check outcome; ``alarm`` is the regression alarm."""

    status: str  # "ok" | "regressed" | "insufficient_live_data" | "no_baseline"
    alarm: bool
    constants_source: str
    constants_reason: str | None
    baseline_accuracy: float | None
    live_accuracy: float | None
    live_pair_count: int
    live_weighted_pair_count: float
    accuracy_drop: float
    min_pairs: int
    response: str
    reasons: tuple[str, ...]
    label_source: str | None = None
    window_days: float | None = None
    per_project: dict[str, float] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """JSON-serializable report; callers stamp timestamps."""
        return {
            "task": "#17201",
            "status": self.status,
            "alarm": self.alarm,
            "constants_source": self.constants_source,
            "constants_reason": self.constants_reason,
            "baseline_accuracy": self.baseline_accuracy,
            "live_accuracy": self.live_accuracy,
            "live_pair_count": self.live_pair_count,
            "live_weighted_pair_count": self.live_weighted_pair_count,
            "accuracy_drop": self.accuracy_drop,
            "min_pairs": self.min_pairs,
            "label_source": self.label_source,
            "window_days": self.window_days,
            "per_project": dict(self.per_project),
            "response": self.response,
            "reasons": list(self.reasons),
        }


def replay_params_from_constants(constants: RecallConstants) -> ReplayParams:
    """Replay the live window under exactly the effective constants."""
    return ReplayParams(
        half_life_days=constants.half_life_days,
        graph_synthetic_discount=constants.graph_synthetic_discount,
        cooccur_alpha=constants.cooccur_alpha,
        cooccur_support_cap=constants.cooccur_support_cap,
    )


def _response_path(source: str) -> str:
    """The documented response to a drift alarm for the active regime."""
    if source == "fitted":
        return (
            "Roll back to the static constants: set "
            "memory.use_fitted_recall_constants=false (one-flag rollback, "
            "#17200). Re-enable only after rerunning the #17198 ship gate "
            "(run_ship_gate_from_store) on fresh labels."
        )
    return (
        "Static constants are the rollback floor and are already active; no "
        "flag to flip. Investigate the recall signal pipeline and rerun the "
        "#17198 ship gate (run_ship_gate_from_store) to re-baseline."
    )


def _baseline_accuracy(
    record: Mapping[str, Any] | None,
    source: str,
    *,
    min_pairs: int,
    load_error: str | None = None,
) -> tuple[float | None, str | None]:
    """Holdout baseline for the active regime, or the reason there is none.

    Fitted constants are baselined against the shipped record's
    ``fitted_eval``; static constants against ``static_eval`` (both were
    scored on the same holdout by ``run_ship_gate``). A holdout below the
    pair floor is no baseline at all.
    """
    if record is None:
        return None, load_error or "no gate decision record to baseline against"
    key = "fitted_eval" if source == "fitted" else "static_eval"
    if source == "fitted" and record.get("ship") is not True:
        return None, "decision record did not ship; there is no fitted holdout baseline"
    eval_block = record.get(key)
    if not isinstance(eval_block, Mapping):
        return None, f"decision record has no {key} block"
    accuracy = eval_block.get("accuracy")
    if (
        isinstance(accuracy, bool)
        or not isinstance(accuracy, int | float)
        or not math.isfinite(float(accuracy))
    ):
        return None, f"decision record {key}.accuracy is not a finite number"
    pair_count = eval_block.get("pair_count")
    if isinstance(pair_count, bool) or not isinstance(pair_count, int) or pair_count < min_pairs:
        return None, (
            f"decision record {key} holdout has {pair_count!r} pairs "
            f"(need {min_pairs}); the baseline would be noise"
        )
    return float(accuracy), None


def evaluate_recall_drift(
    rows: Sequence[ReplayRow],
    *,
    record: Mapping[str, Any] | None,
    constants: RecallConstants,
    thresholds: DriftThresholds | None = None,
    smoothing: float = 1.0,
    clip: float = 10.0,
    record_load_error: str | None = None,
    label_source: str | None = None,
    window_days: float | None = None,
) -> DriftReport:
    """Pure drift check: live-window replay accuracy vs the holdout baseline.

    Propensities are estimated from the window itself (labeled and unlabeled
    rows), mirroring how the ship gate estimated them from its train split.
    """
    thresholds = thresholds if thresholds is not None else DriftThresholds()
    params = replay_params_from_constants(constants)
    propensities = estimate_position_propensities(rows, smoothing=smoothing)
    live = evaluate_pairwise(rows, {}, propensities, default_params=params, clip=clip)
    baseline, baseline_reason = _baseline_accuracy(
        record,
        constants.source,
        min_pairs=thresholds.min_pairs,
        load_error=record_load_error,
    )
    live_accuracy = live.accuracy if live.pair_count > 0 else None

    reasons: list[str] = []
    if baseline is None and baseline_reason is not None:
        reasons.append(baseline_reason)
    if live.pair_count < thresholds.min_pairs:
        reasons.append(
            f"live window has {live.pair_count} labeled pairs "
            f"(need {thresholds.min_pairs}); not enough signal to compare"
        )

    if baseline is None:
        status = "no_baseline"
    elif live.pair_count < thresholds.min_pairs:
        status = "insufficient_live_data"
    else:
        drop = baseline - live.accuracy
        if drop > thresholds.accuracy_drop:
            status = "regressed"
            reasons.append(
                f"live pairwise accuracy {live.accuracy:.4f} fell {drop:.4f} below "
                f"the recorded holdout baseline {baseline:.4f} "
                f"(alarm threshold {thresholds.accuracy_drop:.4f})"
            )
        else:
            status = "ok"
            reasons.append(
                f"live pairwise accuracy {live.accuracy:.4f} is within "
                f"{thresholds.accuracy_drop:.4f} of the holdout baseline {baseline:.4f}"
            )

    return DriftReport(
        status=status,
        alarm=status == "regressed",
        constants_source=constants.source,
        constants_reason=constants.reason,
        baseline_accuracy=baseline,
        live_accuracy=live_accuracy,
        live_pair_count=live.pair_count,
        live_weighted_pair_count=live.weighted_pair_count,
        accuracy_drop=thresholds.accuracy_drop,
        min_pairs=thresholds.min_pairs,
        response=_response_path(constants.source),
        reasons=tuple(reasons),
        label_source=label_source,
        window_days=window_days,
        per_project=dict(live.per_project),
    )


def run_drift_check_from_store(
    store: RecallSignalStore,
    config: MemoryConfig,
    *,
    label_source: str = "digest",
    project_id: str | None = None,
    window_days: float | None = None,
    limit: int = 5000,
    thresholds: DriftThresholds | None = None,
    smoothing: float = 1.0,
    clip: float = 10.0,
) -> DriftReport:
    """Load the recent live window and run the drift check, logging any alarm."""
    constants = resolve_recall_constants(config)
    record, load_error = _load_decision_record(decision_record_path(config))
    window = window_days if window_days is not None else config.recall_drift_window_days
    since = utc_now() - timedelta(days=window)
    rows = [
        replay_row_from_signal_row(row)
        for row in store.fetch_replay_rows(
            label_source=label_source, project_id=project_id, limit=limit, since=since
        )
    ]
    if thresholds is None:
        thresholds = DriftThresholds(accuracy_drop=config.recall_drift_accuracy_drop)
    report = evaluate_recall_drift(
        rows,
        record=record,
        constants=constants,
        thresholds=thresholds,
        smoothing=smoothing,
        clip=clip,
        record_load_error=load_error,
        label_source=label_source,
        window_days=window,
    )
    if report.alarm:
        logger.warning(
            "Recall-quality drift alarm (constants=%s): %s Response: %s",
            report.constants_source,
            " ".join(report.reasons),
            report.response,
        )
    else:
        logger.debug(
            "Recall drift check: status=%s live_pairs=%d baseline=%s",
            report.status,
            report.live_pair_count,
            report.baseline_accuracy,
        )
    return report
