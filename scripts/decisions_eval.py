"""Reproducible evaluation harness for decision consumers (plan 2.1).

Replays shadow records through each configured backend, splits them by
``content_hash``, computes calibration and gate metrics, and writes a Markdown report.

Stub: behavior lands with the Developer. The contract is pinned by
``tests/scripts/test_decisions_eval.py`` and ``tests/scripts/decisions_eval_helpers.py``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from gobby.ai.decisions import DecisionResult, NoulAnswer
from gobby.config.ai import DecisionsConfig


class NoulReplayer(Protocol):
    """The slice of ``DecisionService`` the harness replays through."""

    async def noul(
        self,
        consumer: str,
        state: Mapping[str, Any],
        propositions: Mapping[str, str],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[NoulAnswer]: ...

    def estimate_request_tokens(
        self, state: Mapping[str, Any], questions: Mapping[str, str]
    ) -> int: ...


@dataclass(frozen=True)
class Backend:
    """One backend to evaluate: its configured endpoint and the replaying service."""

    label: str
    config: DecisionsConfig
    service: NoulReplayer


@dataclass(frozen=True)
class BackendReport:
    """Verdict and measurements for one backend over the shared splits."""

    label: str
    model: str | None
    evaluated_backend: str | None
    response_model: str | None
    thresholds: dict[str, float]
    metrics: dict[str, dict[str, Any]]
    cost_per_call_usd: dict[float, float]
    verdict: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class EvalReport:
    """Evaluation of one consumer's dataset across every backend."""

    consumer: str
    splits: dict[str, int]
    backends: tuple[BackendReport, ...]


def split_of(content_hash: str) -> str:
    """Return ``"holdout"`` when ``int(content_hash, 16) % 5 == 0``, else ``"development"``."""
    raise NotImplementedError


def partition(records: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """Drop audit records and group the rest by split."""
    raise NotImplementedError


def select_cohort(records: Sequence[Mapping[str, Any]], size: int) -> list[Mapping[str, Any]]:
    """Return the first ``size`` non-audit records in ascending ``sha256(id)`` order."""
    raise NotImplementedError


def brier_score(predictions: Sequence[tuple[float, bool]]) -> float:
    """Mean squared error between each probability and its 0/1 label."""
    raise NotImplementedError


def expected_calibration_error(predictions: Sequence[tuple[float, bool]], bins: int = 10) -> float:
    """Equal-width-bin ECE over probabilities, micro-averaged over all predictions."""
    raise NotImplementedError


def accuracy_at(predictions: Sequence[tuple[float, bool]], threshold: float) -> float:
    """Accuracy when a prediction is positive at or above ``threshold``."""
    raise NotImplementedError


def selective_metrics(
    predictions: Sequence[tuple[float, bool]], threshold: float
) -> tuple[float | None, float]:
    """Return ``(selective accuracy, coverage)`` for predictions at confidence >= threshold."""
    raise NotImplementedError


def order_flip_rate(pairs: Sequence[tuple[Any, Any]]) -> float:
    """Share of records whose deployed decision differs between original and reversed replay."""
    raise NotImplementedError


def evaluate(
    consumer: str,
    records: Sequence[Mapping[str, Any]],
    backends: Sequence[Backend],
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> EvalReport:
    """Replay ``records`` through each backend and gate the results."""
    raise NotImplementedError


def render_markdown(report: EvalReport, *, dataset_sha256: str) -> str:
    """Render the report as Markdown."""
    raise NotImplementedError


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: Callable[[DecisionsConfig], NoulReplayer] | None = None,
    resolve_secret: Callable[[str], str | None] | None = None,
    load_daemon_decisions: Callable[[], DecisionsConfig] | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    """CLI entry: ``--consumer --dataset --out [--decisions <yaml>]...``."""
    raise NotImplementedError
