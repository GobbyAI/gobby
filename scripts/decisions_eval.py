"""Reproducible evaluation of decision shadow records (plan 2.1).

Replays one JSONL dataset of shadow records through each backend, computes calibration,
ranking, and cascade metrics on a development split and a holdout split, and writes a
Markdown gate report. Thresholds come from the development split only; the holdout is
scored once. Audit records never enter a split, a cohort, or a metric.

Run with ``--consumer tool_rerank`` or ``--consumer found_work``. Each ``--decisions``
file holds one ``ai.decisions`` mapping; without one, the daemon's config is the backend.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from gobby.ai.decisions import DecisionResult, DecisionService, DecisionsUnavailable, NoulAnswer
from gobby.config._loading import load_yaml
from gobby.config.ai import DecisionsConfig
from scripts.decisions_live_capture import resolve_operator_secret

TOOL = "tool_rerank"
FOUND = "found_work"
FOUND_KEY = "found_work"
DEV = "development"
HOLDOUT = "holdout"
GRID = tuple(round(step / 100, 2) for step in range(5, 100, 5))
PRICES = (0.042, 0.24, 0.09)
ECE_MAX = 0.10
FLIP_MAX = 0.10
P95_MAX_MS = 1000.0
ESCALATION_MAX = 0.50
MIN_SUPPORT = 20
EPS = 1e-9
BARS = {
    TOOL: "deployed Recall@k at least the incumbent's",
    FOUND: "cascade false-clear rate at most the incumbent's; escalation rate at most 50%",
}
Pair = tuple[float, bool]


class NoulReplayer(Protocol):
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
    label: str
    config: DecisionsConfig
    service: NoulReplayer


@dataclass(frozen=True)
class BackendReport:
    label: str
    config: DecisionsConfig
    verdict: str
    reasons: tuple[str, ...]
    thresholds: dict[str, float]
    metrics: dict[str, dict[str, float | None]]
    counts: dict[str, tuple[int, int]]
    evaluated_backend: str | None
    response_model: str | None
    estimated_tokens: float
    cost_per_call_usd: dict[float, float]


@dataclass(frozen=True)
class EvaluationReport:
    consumer: str
    sizes: dict[str, int]
    backends: tuple[BackendReport, ...]


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _rate(count: int, total: int) -> float | None:
    return count / total if total else None


def brier_score(pairs: Sequence[Pair]) -> float | None:
    return _mean((probability - float(label)) ** 2 for probability, label in pairs)


def expected_calibration_error(pairs: Sequence[Pair], bins: int = 10) -> float | None:
    if not pairs:
        return None
    counts = [0] * bins
    confidence = [0.0] * bins
    hits = [0.0] * bins
    for probability, label in pairs:
        index = min(int(probability * bins), bins - 1)
        counts[index] += 1
        confidence[index] += probability
        hits[index] += float(label)
    total = len(pairs)
    return sum(
        n / total * abs(hit / n - conf / n)
        for n, conf, hit in zip(counts, confidence, hits, strict=True)
        if n
    )


def accuracy_at(pairs: Sequence[Pair], threshold: float) -> float | None:
    return _mean(float((probability >= threshold) == label) for probability, label in pairs)


def selective_metrics(pairs: Sequence[Pair], threshold: float) -> tuple[float | None, float | None]:
    covered = [(p, y) for p, y in pairs if max(p, 1 - p) >= threshold]
    return accuracy_at(covered, 0.5), _rate(len(covered), len(pairs))


def order_flip_rate(decisions: Sequence[tuple[Sequence[str], Sequence[str]]]) -> float | None:
    return _mean(float(list(forward) != list(reverse)) for forward, reverse in decisions)


def split_of(content_hash: str) -> str:
    return HOLDOUT if int(content_hash, 16) % 5 == 0 else DEV


def _scored(records: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [record for record in records if not record.get("audit", False)]


def partition(records: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    parts: dict[str, list[Mapping[str, Any]]] = {DEV: [], HOLDOUT: []}
    for record in _scored(records):
        parts[split_of(record["content_hash"])].append(record)
    return parts


def select_cohort(records: Sequence[Mapping[str, Any]], size: int) -> list[Mapping[str, Any]]:
    ordered = sorted(
        _scored(records), key=lambda record: hashlib.sha256(record["id"].encode()).hexdigest()
    )
    return ordered[:size]


@dataclass(frozen=True)
class _Reply:
    probabilities: dict[str, float] | None
    identity: str | None = None
    response_model: str | None = None


@dataclass(frozen=True)
class _Row:
    record: Mapping[str, Any]
    forward: _Reply
    reverse: _Reply
    latency_ms: float
    tokens: int


async def _reply(
    service: NoulReplayer, consumer: str, state: Mapping[str, Any], propositions: Mapping[str, str]
) -> _Reply:
    try:
        result = await service.noul(consumer, state, propositions)
    except DecisionsUnavailable:
        return _Reply(None)
    probabilities = {key: result.answers[key].probability for key in propositions}
    return _Reply(probabilities, result.backend_identity, result.response_model)


async def _replay(
    consumer: str,
    records: Sequence[Mapping[str, Any]],
    service: NoulReplayer,
    clock: Callable[[], float],
) -> list[_Row]:
    rows: list[_Row] = []
    for record in _scored(records):
        state = record["state"]
        propositions = record["propositions"]
        reversed_propositions = dict(reversed(list(propositions.items())))
        tokens = service.estimate_request_tokens(state, propositions)
        started = clock()
        forward = await _reply(service, consumer, state, propositions)
        latency_ms = (clock() - started) * 1000.0
        reverse = await _reply(service, consumer, state, reversed_propositions)
        rows.append(_Row(record, forward, reverse, latency_ms, tokens))
    return rows


def _recall(ranked: Sequence[str], gold: set[str], k: int) -> float:
    return len(set(ranked[:k]) & gold) / len(gold)


def _incumbent(record: Mapping[str, Any]) -> list[str]:
    return list(record["incumbent"]["verdict"])


def _deployed(row: _Row, minimum: float, *, reverse: bool = False) -> list[str]:
    reply = row.reverse if reverse else row.forward
    if reply.probabilities is None:
        return _incumbent(row.record)
    probabilities = reply.probabilities
    kept = [c for c in row.record["candidates"] if probabilities[c] >= minimum]
    return sorted(kept, key=lambda c: -probabilities[c])


def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def _common(
    rows: Sequence[_Row],
    pairs: Sequence[Pair],
    flips: Sequence[tuple[Sequence[str], Sequence[str]]],
) -> dict[str, float | None]:
    return {
        "ece": expected_calibration_error(pairs),
        "brier": brier_score(pairs),
        "order_flip_rate": order_flip_rate(flips),
        "p95_latency_ms": _p95([row.latency_ms for row in rows]),
    }


def _tool_metrics(rows: Sequence[_Row], minimum: float) -> dict[str, float | None]:
    deployed: list[float] = []
    incumbent: list[float] = []
    paired: list[float] = []
    pairs: list[Pair] = []
    for row in rows:
        gold = set(row.record["gold_relevant"])
        if not gold:
            continue
        k = row.record["k"]
        recall = _recall(_deployed(row, minimum), gold, k)
        baseline = _recall(_incumbent(row.record), gold, k)
        deployed.append(recall)
        incumbent.append(baseline)
        if row.forward.probabilities is not None:
            paired.append(recall - baseline)
    for row in rows:
        probabilities = row.forward.probabilities
        if probabilities is None:
            continue
        gold = set(row.record["gold_relevant"])
        pairs.extend((probabilities[c], c in gold) for c in row.record["candidates"])
    empty = [row for row in rows if not row.record["gold_relevant"]]
    metrics: dict[str, float | None] = {
        "recall_at_k": _mean(deployed),
        "incumbent_recall_at_k": _mean(incumbent),
        "recall_at_k_delta": _mean(d - i for d, i in zip(deployed, incumbent, strict=True)),
        "paired_recall_at_k_delta": _mean(paired),
        "reject_all_accuracy": _mean(float(not _deployed(row, minimum)) for row in empty),
        "accuracy": accuracy_at(pairs, 0.5),
    }
    for threshold in GRID:
        accuracy, coverage = selective_metrics(pairs, threshold)
        metrics[f"selective_accuracy@{threshold:.2f}"] = accuracy
        metrics[f"selective_coverage@{threshold:.2f}"] = coverage
    flips = [(_deployed(row, minimum), _deployed(row, minimum, reverse=True)) for row in rows]
    metrics.update(_common(rows, pairs, flips))
    return metrics


def _prob(reply: _Reply) -> float | None:
    if reply.probabilities is None:
        return None
    return reply.probabilities[FOUND_KEY]


def _cascade(
    probability: float | None, verdict: bool | None, below: float, above: float
) -> tuple[bool, bool]:
    """Return ``(alert, escalated)`` for one record under the cascade."""
    if probability is not None:
        if probability >= above:
            return True, False
        if probability <= below:
            return False, False
    return verdict is not False, True


def _found_metrics(rows: Sequence[_Row], below: float, above: float) -> dict[str, float | None]:
    final = [
        (row, *_cascade(_prob(row.forward), row.record["incumbent"]["verdict"], below, above))
        for row in rows
    ]
    shirk = [item for item in final if item[0].record["gold_shirk"]]
    clean = [item for item in final if not item[0].record["gold_shirk"]]
    pairs: list[Pair] = []
    for row in rows:
        probability = _prob(row.forward)
        if probability is not None:
            pairs.append((probability, bool(row.record["gold_shirk"])))
    flips = []
    for row in rows:
        forward_alert, _ = _cascade(
            _prob(row.forward), row.record["incumbent"]["verdict"], below, above
        )
        reverse_alert, _ = _cascade(
            _prob(row.reverse), row.record["incumbent"]["verdict"], below, above
        )
        flips.append(
            (["alert" if forward_alert else "clear"], ["alert" if reverse_alert else "clear"])
        )
    metrics: dict[str, float | None] = {
        "false_clear_rate": _rate(sum(1 for _, alert, _ in shirk if not alert), len(shirk)),
        "incumbent_false_clear_rate": _rate(
            sum(1 for row, _, _ in shirk if row.record["incumbent"]["verdict"] is False), len(shirk)
        ),
        "false_alert_rate": _rate(sum(1 for _, alert, _ in clean if alert), len(clean)),
        "escalation_rate": _rate(sum(1 for _, _, escalated in final if escalated), len(final)),
    }
    metrics.update(_common(rows, pairs, flips))
    return metrics


def _thresholds(consumer: str, dev: Sequence[_Row]) -> dict[str, float] | None:
    if consumer == TOOL:
        return _tool_thresholds(dev)
    return _found_thresholds(dev)


def _tool_thresholds(dev: Sequence[_Row]) -> dict[str, float] | None:
    incumbent = _tool_metrics(dev, GRID[0])["incumbent_recall_at_k"]
    if incumbent is None:
        return None
    choices: list[tuple[float, float]] = []
    for minimum in GRID:
        metrics = _tool_metrics(dev, minimum)
        recall = metrics["recall_at_k"]
        if recall is not None and recall + EPS >= incumbent:
            choices.append((-(metrics["reject_all_accuracy"] or 0.0), minimum))
    if not choices:
        return None
    return {"min_probability": min(choices)[1]}


def _found_thresholds(dev: Sequence[_Row]) -> dict[str, float] | None:
    incumbent = _found_metrics(dev, GRID[0], GRID[-1])["incumbent_false_clear_rate"]
    if incumbent is None:
        return None
    choices: list[tuple[float, int, float, float]] = []
    for i, below in enumerate(GRID):
        for j in range(i + 1, len(GRID)):
            metrics = _found_metrics(dev, below, GRID[j])
            clear = metrics["false_clear_rate"]
            escalation = metrics["escalation_rate"]
            if clear is not None and escalation is not None and clear <= incumbent + EPS:
                choices.append((escalation, i - j, below, GRID[j]))
    if not choices:
        return None
    _, _, below, above = min(choices)
    return {"accept_below": below, "accept_above": above}


def _split_metrics(
    consumer: str, rows: Sequence[_Row], thresholds: Mapping[str, float]
) -> dict[str, float | None]:
    if consumer == TOOL:
        return _tool_metrics(rows, thresholds["min_probability"])
    return _found_metrics(rows, thresholds["accept_below"], thresholds["accept_above"])


def _class_names(consumer: str) -> tuple[str, str]:
    if consumer == TOOL:
        return "gold-relevant", "gold-empty"
    return "gold-shirk", "gold-clean"


def _class_counts(consumer: str, rows: Sequence[_Row]) -> tuple[int, int]:
    if consumer == TOOL:
        positive = sum(1 for row in rows if row.record["gold_relevant"])
    else:
        positive = sum(1 for row in rows if row.record["gold_shirk"])
    return positive, len(rows) - positive


def _gate_keys(consumer: str) -> tuple[str, ...]:
    common = ("ece", "order_flip_rate", "p95_latency_ms")
    if consumer == TOOL:
        return (*common, "recall_at_k", "incumbent_recall_at_k")
    return (*common, "false_clear_rate", "incumbent_false_clear_rate", "escalation_rate")


def _gate(
    consumer: str, dev: Sequence[_Row], holdout: Sequence[_Row], metrics: Mapping[str, float | None]
) -> list[str]:
    short: list[str] = []
    for split, rows in ((DEV, dev), (HOLDOUT, holdout)):
        for label, count in zip(_class_names(consumer), _class_counts(consumer, rows), strict=True):
            if count < MIN_SUPPORT:
                short.append(f"{split} {label} {count} < {MIN_SUPPORT}")
    short += [
        f"undefined {key} on {HOLDOUT}" for key in _gate_keys(consumer) if metrics[key] is None
    ]
    reasons: list[str] = []
    if short:
        reasons.append("insufficient support: " + "; ".join(short))
    ece = metrics["ece"]
    if ece is not None and ece > ECE_MAX + EPS:
        reasons.append(f"ECE {ece:.4f} exceeds {ECE_MAX:.2f} ({HOLDOUT})")
    flip = metrics["order_flip_rate"]
    if flip is not None and flip > FLIP_MAX + EPS:
        reasons.append(f"order-flip rate {flip:.1%} exceeds {FLIP_MAX:.0%} ({HOLDOUT})")
    p95 = metrics["p95_latency_ms"]
    if p95 is not None and p95 > P95_MAX_MS + EPS:
        reasons.append(f"p95 latency {p95:.0f} ms exceeds {P95_MAX_MS:.0f} ms ({HOLDOUT})")
    if consumer == TOOL:
        recall = metrics["recall_at_k"]
        baseline = metrics["incumbent_recall_at_k"]
        if recall is not None and baseline is not None and recall + EPS < baseline:
            reasons.append(f"Recall@k {recall:.4f} below incumbent {baseline:.4f} ({HOLDOUT})")
    else:
        clear = metrics["false_clear_rate"]
        baseline = metrics["incumbent_false_clear_rate"]
        if clear is not None and baseline is not None and clear > baseline + EPS:
            reasons.append(
                f"false-clear rate {clear:.4f} exceeds incumbent {baseline:.4f} ({HOLDOUT})"
            )
        escalation = metrics["escalation_rate"]
        if escalation is not None and escalation > ESCALATION_MAX + EPS:
            reasons.append(f"escalation rate {escalation:.2%} exceeds 50% ({HOLDOUT})")
    return reasons


def _identity(rows: Sequence[_Row]) -> tuple[str | None, str | None, list[str]]:
    replies = [
        reply
        for row in rows
        for reply in (row.forward, row.reverse)
        if reply.probabilities is not None
    ]
    if not replies:
        return None, None, ["mixed or unverified backend: no available answers"]
    identities = {reply.identity for reply in replies}
    models = {reply.response_model for reply in replies}
    if None in identities or None in models:
        return None, None, ["mixed or unverified backend: answers without a backend identity"]
    if len(identities) > 1 or len(models) > 1:
        return (
            None,
            None,
            ["mixed or unverified backend: answers span identities or response models"],
        )
    return identities.pop(), models.pop(), []


def _evaluate_backend(
    consumer: str,
    records: Sequence[Mapping[str, Any]],
    backend: Backend,
    clock: Callable[[], float],
) -> BackendReport:
    rows = asyncio.run(_replay(consumer, records, backend.service, clock))
    parts = {DEV: [], HOLDOUT: []}
    for row in rows:
        parts[split_of(row.record["content_hash"])].append(row)
    dev, holdout = parts[DEV], parts[HOLDOUT]
    identity, model, reasons = _identity(rows)
    thresholds = _thresholds(consumer, dev)
    metrics: dict[str, dict[str, float | None]] = {}
    if thresholds is None:
        reasons.append("no threshold meets the development bar")
    else:
        metrics = {
            DEV: _split_metrics(consumer, dev, thresholds),
            HOLDOUT: _split_metrics(consumer, holdout, thresholds),
        }
        reasons.extend(_gate(consumer, dev, holdout, metrics[HOLDOUT]))
    tokens = _mean(float(row.tokens) for row in rows) or 0.0
    return BackendReport(
        label=backend.label,
        config=backend.config,
        verdict="FAIL" if reasons else "PASS",
        reasons=tuple(reasons),
        thresholds=thresholds or {},
        metrics=metrics,
        counts={DEV: _class_counts(consumer, dev), HOLDOUT: _class_counts(consumer, holdout)},
        evaluated_backend=identity,
        response_model=model,
        estimated_tokens=tokens,
        cost_per_call_usd={price: tokens * price / 1_000_000 for price in PRICES},
    )


def evaluate(
    consumer: str,
    records: Sequence[Mapping[str, Any]],
    backends: Sequence[Backend],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> EvaluationReport:
    scored = _scored(records)
    sizes = {split: len(rows) for split, rows in partition(scored).items()}
    reports = tuple(_evaluate_backend(consumer, scored, backend, clock) for backend in backends)
    return EvaluationReport(consumer=consumer, sizes=sizes, backends=reports)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _table_rows(consumer: str) -> list[tuple[str, Callable[[BackendReport], str]]]:
    def holdout(key: str) -> Callable[[BackendReport], str]:
        return lambda report: _fmt(report.metrics.get(HOLDOUT, {}).get(key))

    rows: list[tuple[str, Callable[[BackendReport], str]]] = [
        ("Verdict", lambda report: report.verdict),
        (f"ECE ({HOLDOUT}, limit {ECE_MAX:.2f})", holdout("ece")),
        (f"Order-flip rate ({HOLDOUT}, limit {FLIP_MAX:.0%})", holdout("order_flip_rate")),
        (f"p95 latency ms ({HOLDOUT}, limit {P95_MAX_MS:.0f})", holdout("p95_latency_ms")),
        (f"Brier ({HOLDOUT})", holdout("brier")),
    ]
    if consumer == TOOL:
        rows.append((f"Recall@k ({HOLDOUT})", holdout("recall_at_k")))
        rows.append((f"Incumbent Recall@k ({HOLDOUT})", holdout("incumbent_recall_at_k")))
    else:
        rows.append((f"False-clear rate ({HOLDOUT})", holdout("false_clear_rate")))
        rows.append(
            (f"Incumbent false-clear rate ({HOLDOUT})", holdout("incumbent_false_clear_rate"))
        )
        rows.append((f"Escalation rate ({HOLDOUT}, limit 50%)", holdout("escalation_rate")))
    for price in PRICES:
        rows.append(
            (
                f"Cost per call at ${price} per M input tokens",
                lambda report, price=price: f"${report.cost_per_call_usd[price]:.6f}",
            )
        )
    return rows


def render(report: EvaluationReport, dataset_name: str, dataset_sha256: str) -> str:
    lines = [
        f"# Decisions eval: {report.consumer}",
        "",
        f"- Dataset: `{dataset_name}`, sha256 `{dataset_sha256}`",
        f"- Splits: {DEV} {report.sizes[DEV]} records, {HOLDOUT} {report.sizes[HOLDOUT]} records",
        f"- Consumer bar: {BARS[report.consumer]}",
        f"- Gate: ECE <= {ECE_MAX:.2f}; order-flip rate <= {FLIP_MAX:.0%}; p95 latency <= "
        f"{P95_MAX_MS:.0f} ms; at least {MIN_SUPPORT} records per gold class in each split.",
        "",
    ]
    for backend in report.backends:
        host = urlparse(backend.config.api_base or "").hostname or ""
        lines += [
            f"## {backend.label}",
            "",
            f"- Configured model: `{backend.config.model}`",
            f"- Wire: `{backend.config.wire_api}`",
            f"- Endpoint host: `{host}`",
            f"- evaluated_backend: `{backend.evaluated_backend}`",
            f"- response_model: `{backend.response_model}`",
            "- Thresholds: "
            + (", ".join(f"{k}={v:.2f}" for k, v in backend.thresholds.items()) or "none"),
            f"- Estimated input tokens per call: {backend.estimated_tokens:.0f}",
            f"- Verdict: **{backend.verdict}**",
        ]
        lines += [f"- Reason: {reason}" for reason in backend.reasons]
        lines.append("")
    labels = [backend.label for backend in report.backends]
    lines += [
        "## Side by side",
        "",
        "| Measure | " + " | ".join(labels) + " |",
        "| --- |" + " --- |" * len(labels),
    ]
    for name, cell in _table_rows(report.consumer):
        lines.append(
            f"| {name} | " + " | ".join(cell(backend) for backend in report.backends) + " |"
        )
    return "\n".join(lines) + "\n"


def _default_service(config: DecisionsConfig) -> NoulReplayer:
    return DecisionService(config)


def _load_daemon_decisions() -> DecisionsConfig:
    """Return the daemon's active ``ai.decisions``, read from the hub as the daemon reads it."""
    from gobby.storage.config_repository import ConfigRepository
    from gobby.storage.hub.runtime import runtime_hub_database

    with runtime_hub_database(apply_migrations=False) as db:
        repository = ConfigRepository(db)
        snapshot = repository.read()
        candidate = repository.runtime_candidate(dict(snapshot.overrides), snapshot.secret_bindings)
        return candidate.ai.decisions


def _file_backend(
    path: Path,
    resolve_secret: Callable[[str], str | None],
    make_service: Callable[[DecisionsConfig], NoulReplayer],
) -> Backend:
    config = DecisionsConfig.model_validate(load_yaml(str(path), secret_resolver=resolve_secret))
    return Backend(path.stem, config, make_service(config))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consumer", required=True, choices=(TOOL, FOUND))
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--decisions", type=Path, action="append", default=[])
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: Callable[[DecisionsConfig], NoulReplayer] | None = None,
    resolve_secret: Callable[[str], str | None] | None = None,
    load_daemon_decisions: Callable[[], DecisionsConfig] | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    dataset_bytes = args.dataset.read_bytes()
    records = [
        json.loads(line) for line in dataset_bytes.decode("utf-8").splitlines() if line.strip()
    ]
    make_service = service_factory or _default_service
    secret = resolve_secret or resolve_operator_secret
    if args.decisions:
        backends = [_file_backend(path, secret, make_service) for path in args.decisions]
    else:
        config = (load_daemon_decisions or _load_daemon_decisions)()
        backends = [Backend("daemon", config, make_service(config))]
    report = evaluate(args.consumer, records, backends, clock=clock or time.monotonic)
    digest = hashlib.sha256(dataset_bytes).hexdigest()
    args.out.write_text(render(report, args.dataset.name, digest), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
