"""Decision evaluation harness: splits, metrics, gates, report, and backend comparison (2.1.2-2.1.7).

Expected values are hand-computed from the datasets in ``decisions_eval_helpers``. Each
dataset comment gives the arithmetic.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from gobby.config.ai import DecisionsConfig
from scripts import decisions_eval as ev
from tests.scripts.decisions_eval_helpers import (
    DEV,
    HOLDOUT,
    IDENTITY_CLEF,
    IDENTITY_JEV,
    MODEL_CLEF,
    MODEL_JEV,
    RESPONSE_CLEF,
    RESPONSE_JEV,
    SECRET_NAME,
    SECRET_VALUE,
    TOKENS_PER_CALL,
    Dataset,
    FakeClock,
    build_found,
    build_tool,
    decisions_config,
    write_jsonl,
)

TOOL = "tool_rerank"
FOUND = "found_work"


def resolve_secret(name: str) -> str:
    assert name == SECRET_NAME, name
    return SECRET_VALUE


def test_metrics_on_known_dataset() -> None:
    # Brier: five positives and five negatives at p=0.9 -> (5*0.01 + 5*0.81) / 10.
    # ECE: all ten fall in the 0.9 bin, so |mean label 0.5 - mean probability 0.9|.
    split_evenly = [(0.9, True)] * 5 + [(0.9, False)] * 5
    assert ev.brier_score(split_evenly) == pytest.approx(0.41)
    assert ev.expected_calibration_error(split_evenly) == pytest.approx(0.4)

    # Accuracy at 0.5 predicts positive for p >= 0.5: two of four are right.
    # Selective at 0.75 covers confidence max(p, 1-p) >= 0.75: the 0.8 and 0.2 rows.
    # Of those, the 0.8 row is right and the 0.2 row is wrong.
    mixed = [(0.8, True), (0.6, False), (0.3, False), (0.2, True)]
    assert ev.accuracy_at(mixed, 0.5) == pytest.approx(0.5)
    accuracy, coverage = ev.selective_metrics(mixed, 0.75)
    assert accuracy == pytest.approx(0.5)
    assert coverage == pytest.approx(0.5)

    # Order flip: one of four records changes its deployed decision under reversal.
    decisions = [
        (["a", "b"], ["a", "b"]),
        (["a", "b"], ["b", "a"]),
        (["a"], ["a"]),
        ([], []),
    ]
    assert ev.order_flip_rate(decisions) == pytest.approx(0.25)

    # Split by content_hash: a duplicate keeps its hash, so both copies share a split.
    ds = Dataset()
    first = ds.tool(HOLDOUT, gold=["a"], incumbent=["a", "b"], forward={"a": 0.95, "b": 0.05})
    ds.duplicate(first)
    ds.tool(DEV, gold=[], incumbent=["a", "b"], forward={"a": 0.05, "b": 0.05})
    copies = [r for r in ds.records if r["state"]["request"] == first]
    assert len(copies) == 2
    assert copies[0]["id"] != copies[1]["id"]
    assert ev.split_of(copies[0]["content_hash"]) == HOLDOUT
    assert ev.split_of(copies[1]["content_hash"]) == HOLDOUT
    parts = ev.partition(ds.records)
    holdout_ids = {r["id"] for r in parts[HOLDOUT]}
    assert {c["id"] for c in copies} <= holdout_ids

    # Cohort: ascending sha256(id), audit excluded, and both copies can be selected.
    cohort = ev.select_cohort(ds.records, 2)
    expected = sorted(ds.records, key=lambda r: hashlib.sha256(r["id"].encode()).hexdigest())
    assert [r["id"] for r in cohort] == [r["id"] for r in expected[:2]]
    everyone = ev.select_cohort(ds.records, len(ds.records))
    assert {c["id"] for c in copies} <= {r["id"] for r in everyone}


def test_report_identifies_run(tmp_path: Path) -> None:
    clock = FakeClock()
    ds = Dataset(clock)
    build_tool(ds, DEV)
    build_tool(ds, HOLDOUT)
    dataset = write_jsonl(ds.records, tmp_path / "tool.jsonl")
    out = tmp_path / "report.md"

    ev.main(
        ["--consumer", TOOL, "--dataset", str(dataset), "--out", str(out)],
        service_factory=lambda _config: ds.service(),
        resolve_secret=resolve_secret,
        load_daemon_decisions=lambda: decisions_config(MODEL_CLEF),
        clock=clock,
    )

    text = out.read_text()
    assert hashlib.sha256(dataset.read_bytes()).hexdigest() in text
    assert MODEL_CLEF in text
    assert RESPONSE_CLEF in text
    assert any("evaluated_backend" in line and IDENTITY_CLEF in line for line in text.splitlines())
    assert DEV in text
    assert HOLDOUT in text
    assert "Recall@k" in text


# Each row: consumer, holdout kwargs for its builder, and the reason token a breach must name.
# ``None`` means every gate holds.
GATE_CASES = [
    pytest.param(TOOL, {}, None, id="tool-passes"),
    pytest.param(FOUND, {}, None, id="found-passes"),
    pytest.param(TOOL, {"empty_a": 0.95}, "ECE", id="tool-ece"),
    pytest.param(TOOL, {"flips": 25}, "order-flip", id="tool-order-flip"),
    pytest.param(TOOL, {"latency_ms": 1500.0}, "p95", id="tool-p95"),
    pytest.param(TOOL, {"misses": 3}, "Recall@k", id="tool-recall"),
    pytest.param(TOOL, {"gold": 19}, "insufficient support", id="tool-support"),
    pytest.param(FOUND, {"false_high": 25, "false_confident": 0}, "ECE", id="found-ece"),
    pytest.param(FOUND, {"latency_ms": 1500.0}, "p95", id="found-p95"),
    pytest.param(FOUND, {"true_confident": 19}, "insufficient support", id="found-support"),
    pytest.param(
        FOUND,
        {"true_confident": 22, "true_clear": 3},
        "false-clear",
        id="found-false-clear",
    ),
    pytest.param(
        FOUND,
        {"true_confident": 10, "true_escalated": 15, "false_confident": 10, "false_escalated": 15},
        "escalation",
        id="found-escalation",
    ),
]


def _gate_dataset(consumer: str, holdout: dict[str, Any]) -> Dataset:
    ds = Dataset()
    if consumer == TOOL:
        build_tool(ds, DEV)
        build_tool(ds, HOLDOUT, **holdout)
    else:
        build_found(ds, DEV)
        build_found(ds, HOLDOUT, **holdout)
    return ds


@pytest.mark.parametrize(("consumer", "holdout", "token"), GATE_CASES)
def test_gate_verdicts_on_synthetic_sets(
    consumer: str, holdout: dict[str, Any], token: str | None
) -> None:
    # Dev selects thresholds: tool min_probability 0.10 (lowest with max reject-all), found pair
    # (0.05, 0.95) (zero escalation everywhere, so the widest band wins). A breached set moves
    # only one constant, so exactly one reason names it.
    ds = _gate_dataset(consumer, holdout)
    backend = ev.evaluate(consumer, ds.records, [ds.backend("clef")], clock=ds.clock).backends[0]
    if token is None:
        assert backend.verdict == "PASS"
        assert backend.reasons == ()
    else:
        assert backend.verdict == "FAIL"
        assert len(backend.reasons) == 1
        assert token in backend.reasons[0]


def _evaluate_edge(
    consumer: str,
    build: Callable[[Dataset], None],
    identity_for: Callable[[int], str | None] | None = None,
) -> ev.BackendReport:
    clock = FakeClock()
    ds = Dataset(clock)
    build(ds)
    backend = ds.backend("clef", identity_for=identity_for)
    return ev.evaluate(consumer, ds.records, [backend], clock=clock).backends[0]


def _found_null_incumbent(ds: Dataset) -> None:
    build_found(ds, DEV)
    ds.found(HOLDOUT, gold_shirk=False, incumbent=None, probability=0.5)
    ds.found(HOLDOUT, gold_shirk=False, incumbent=False, probability=0.5)


def test_found_null_incumbent_escalates_and_alerts() -> None:
    report = _evaluate_edge(FOUND, _found_null_incumbent)
    # The null verdict is escalated and counts as an alert; the False verdict clears.
    assert report.metrics[HOLDOUT]["false_alert_rate"] == pytest.approx(0.5)
    assert report.metrics[HOLDOUT]["escalation_rate"] == pytest.approx(1.0)


def _found_unavailable(ds: Dataset) -> None:
    build_found(ds, DEV)
    ds.found(HOLDOUT, gold_shirk=True, incumbent=False, probability=None)
    ds.found(HOLDOUT, gold_shirk=True, incumbent=True, probability=0.95)


def test_found_unavailable_escalates_and_clears() -> None:
    report = _evaluate_edge(FOUND, _found_unavailable)
    # The unavailable classifier escalates to the incumbent's False verdict, which clears.
    holdout = report.metrics[HOLDOUT]
    assert holdout["escalation_rate"] == pytest.approx(0.5)
    assert holdout["false_clear_rate"] == pytest.approx(0.5)
    assert holdout["incumbent_false_clear_rate"] == pytest.approx(0.5)


def _tool_fallback(ds: Dataset) -> None:
    # 10 available rows: deployed [a, b] recalls 1.0, incumbent [a, c] recalls 0.5, delta 0.5.
    # 30 unavailable rows fall back to the incumbent order, delta 0.
    # Deployed mean = (10 * 1.0 + 30 * 0.5) / 40 = 0.625. Incumbent mean = 0.5.
    # Delta = 0.125 = paired delta 0.5 * available fraction 10/40.
    build_tool(ds, DEV)
    for _ in range(10):
        ds.tool(
            HOLDOUT,
            gold=["a", "b"],
            incumbent=["a", "c"],
            forward={"a": 0.95, "b": 0.95, "c": 0.05},
            candidates=("a", "b", "c"),
        )
    for _ in range(30):
        ds.tool(
            HOLDOUT,
            gold=["a", "b"],
            incumbent=["a", "c"],
            forward=None,
            candidates=("a", "b", "c"),
        )


def test_tool_fallback_scales_delta_by_available_fraction() -> None:
    report = _evaluate_edge(TOOL, _tool_fallback)
    holdout = report.metrics[HOLDOUT]
    assert holdout["recall_at_k"] == pytest.approx(0.625)
    assert holdout["incumbent_recall_at_k"] == pytest.approx(0.5)
    assert holdout["recall_at_k_delta"] == pytest.approx(0.125)
    assert holdout["paired_recall_at_k_delta"] == pytest.approx(0.5)


def _tool_trails(ds: Dataset) -> None:
    # 10 available rows rank only the non-gold c: recall 0 against incumbent 1.0, so paired -1.
    # 30 unavailable rows fall back and match. Delta = (10 * -1) / 40 = -0.25.
    build_tool(ds, DEV)
    for _ in range(10):
        ds.tool(
            HOLDOUT,
            gold=["a", "b"],
            incumbent=["a", "b"],
            forward={"a": 0.05, "b": 0.05, "c": 0.95},
            candidates=("a", "b", "c"),
        )
    for _ in range(30):
        ds.tool(
            HOLDOUT,
            gold=["a", "b"],
            incumbent=["a", "b"],
            forward=None,
            candidates=("a", "b", "c"),
        )


def test_tool_trails_incumbent_fails_on_recall() -> None:
    report = _evaluate_edge(TOOL, _tool_trails)
    assert report.verdict == "FAIL"
    assert any("Recall@k" in reason for reason in report.reasons)
    assert report.metrics[HOLDOUT]["recall_at_k_delta"] == pytest.approx(-0.25)
    assert report.metrics[HOLDOUT]["paired_recall_at_k_delta"] == pytest.approx(-1.0)


def _tool_k_exceeds(ds: Dataset) -> None:
    # One row, k=5, list [a], gold {a, b}: recall is |{a}| / |gold| = 0.5, not |{a}| / 5.
    build_tool(ds, DEV)
    ds.tool(HOLDOUT, gold=["a", "b"], incumbent=["a"], forward={"a": 0.95}, candidates=("a",), k=5)


def test_tool_k_exceeds_list_uses_gold_denominator() -> None:
    report = _evaluate_edge(TOOL, _tool_k_exceeds)
    assert report.metrics[HOLDOUT]["recall_at_k"] == pytest.approx(0.5)


def _found_zero_denominator(ds: Dataset) -> None:
    # No gold-shirk rows in holdout: false-clear rate is undefined.
    build_found(ds, DEV)
    ds.found(HOLDOUT, gold_shirk=False, incumbent=False, probability=0.5)
    ds.found(HOLDOUT, gold_shirk=False, incumbent=False, probability=0.5)


def test_found_zero_denominator_reports_insufficient_support() -> None:
    report = _evaluate_edge(FOUND, _found_zero_denominator)
    assert report.metrics[HOLDOUT]["false_clear_rate"] is None
    assert any("insufficient support" in reason for reason in report.reasons)


def _tool_short_support(ds: Dataset) -> None:
    build_tool(ds, DEV)
    build_tool(ds, HOLDOUT, empty=19)


def test_tool_short_support_fails_closed() -> None:
    report = _evaluate_edge(TOOL, _tool_short_support)
    assert any("insufficient support" in reason for reason in report.reasons)


def _tool_dev_only_threshold(ds: Dataset) -> None:
    # Dev alone selects 0.10. Holdout alone would select 0.15, because the one holdout empty row
    # scoring a=0.12 stays in the deployed list at 0.10. Reject-all on holdout is therefore 24/25.
    build_tool(ds, DEV)
    build_tool(ds, HOLDOUT, empty=24)
    ds.tool(HOLDOUT, gold=[], incumbent=["a", "b"], forward={"a": 0.12, "b": 0.05})


def test_tool_threshold_comes_from_dev_only() -> None:
    report = _evaluate_edge(TOOL, _tool_dev_only_threshold)
    assert report.thresholds["min_probability"] == pytest.approx(0.10)
    assert report.metrics[HOLDOUT]["reject_all_accuracy"] == pytest.approx(24 / 25)


def _tool_base(ds: Dataset) -> None:
    build_tool(ds, DEV)
    build_tool(ds, HOLDOUT)


def test_tool_identity_switch_fails_closed() -> None:
    report = _evaluate_edge(
        TOOL,
        _tool_base,
        identity_for=lambda index: IDENTITY_CLEF if index < 50 else IDENTITY_JEV,
    )
    assert report.verdict == "FAIL"
    assert any("mixed or unverified backend" in reason for reason in report.reasons)


def test_tool_identity_missing_fails_closed() -> None:
    report = _evaluate_edge(TOOL, _tool_base, identity_for=lambda _index: None)
    assert report.verdict == "FAIL"
    assert any("mixed or unverified backend" in reason for reason in report.reasons)


def test_gate_uses_representative_cohort() -> None:
    # Skewed holdout: 40 gold-shirk rows (all confirmed) and 160 gold-clean rows.
    # Ten gold-clean rows (5 duplicated pairs) score 0.95, so the classifier disagrees with the
    # incumbent on 10 of 200 cohort rows (5%). Fifty audit rows would be false clears if counted.
    # ECE: 50 rows at 0.95 with 40 positives: 0.25 * |0.8 - 0.95| = 0.0375.
    #      150 rows at 0.05, all negative: 0.75 * 0.05 = 0.0375. Total 0.075.
    clock = FakeClock()
    ds = Dataset(clock)
    build_found(ds, DEV)
    build_found(ds, HOLDOUT, true_confident=40, false_confident=150)
    copies = []
    for _ in range(5):
        request = ds.found(HOLDOUT, gold_shirk=False, incumbent=False, probability=0.95)
        ds.duplicate(request)
        copies.append(request)
    for _ in range(50):
        ds.found(HOLDOUT, gold_shirk=True, incumbent=True, probability=0.05, audit=True)

    backend = ev.evaluate(FOUND, ds.records, [ds.backend("clef")], clock=clock).backends[0]

    holdout = backend.metrics[HOLDOUT]
    assert backend.verdict == "PASS"
    assert holdout["false_clear_rate"] == pytest.approx(0.0)
    assert holdout["incumbent_false_clear_rate"] == pytest.approx(0.0)
    assert holdout["escalation_rate"] == pytest.approx(0.0)
    assert holdout["false_alert_rate"] == pytest.approx(10 / 160)
    assert holdout["ece"] == pytest.approx(0.075)

    holdout_ids = {r["id"] for r in ev.partition(ds.records)[HOLDOUT]}
    for request in copies:
        ids = [r["id"] for r in ds.records if r["state"]["request"] == request]
        assert len(ids) == 2
        assert set(ids) <= holdout_ids


def test_compares_backends_side_by_side(tmp_path: Path) -> None:
    clock = FakeClock()
    clef = Dataset(clock)
    build_tool(clef, DEV)
    build_tool(clef, HOLDOUT)
    jev = Dataset(clock)
    build_tool(jev, DEV)
    # Jev is the same rows scored with an overconfident 0.95 on empty rows, so its holdout ECE
    # breaches while Clef's passes.
    build_tool(jev, HOLDOUT, empty_a=0.95)
    assert clef.records == jev.records

    clef_service = clef.service()
    jev_service = jev.service(identity=IDENTITY_JEV, response_model=RESPONSE_JEV)
    report = ev.evaluate(
        TOOL,
        clef.records,
        [
            ev.Backend("clef", decisions_config(MODEL_CLEF), clef_service),
            ev.Backend("jev", decisions_config(MODEL_JEV, "openrouter-decisions"), jev_service),
        ],
        clock=clock,
    )
    clef_report, jev_report = report.backends

    # Each backend keeps its own identity, response model, thresholds, and verdict.
    assert clef_report.evaluated_backend == IDENTITY_CLEF
    assert clef_report.response_model == RESPONSE_CLEF
    assert jev_report.evaluated_backend == IDENTITY_JEV
    assert jev_report.response_model == RESPONSE_JEV
    assert "min_probability" in clef_report.thresholds
    assert "min_probability" in jev_report.thresholds
    assert clef_report.verdict == "PASS"
    assert jev_report.verdict == "FAIL"
    assert any("ECE" in reason for reason in jev_report.reasons)

    # Both backends replay the same records over identical splits.
    assert len(clef_service.calls) == len(jev_service.calls)
    assert {rid for rid, _ in clef_service.calls} == {rid for rid, _ in jev_service.calls}

    # Per-call cost at each pinned hosted price, from the estimated input tokens.
    for backend_report in report.backends:
        for price in (0.042, 0.24, 0.09):
            expected = TOKENS_PER_CALL * price / 1_000_000
            assert backend_report.cost_per_call_usd[price] == pytest.approx(expected)

    # The CLI path: two --decisions files, one of which carries a $secret: api_key.
    clef_yaml = tmp_path / "clef.yaml"
    clef_yaml.write_text(
        "api_base: https://clef.decide.test\n"
        "wire_api: workers-ai\n"
        f"model: {MODEL_CLEF}\n"
        "identity_contract: response_version\n"
        f"api_key: $secret:{SECRET_NAME}\n"
    )
    jev_yaml = tmp_path / "jev.yaml"
    jev_yaml.write_text(
        "api_base: https://jev.decide.test\n"
        "wire_api: openrouter-decisions\n"
        f"model: {MODEL_JEV}\n"
        "identity_contract: response_version\n"
    )
    dataset = write_jsonl(clef.records, tmp_path / "tool.jsonl")
    seen: dict[str, DecisionsConfig] = {}
    services = {
        MODEL_CLEF: clef.service(),
        MODEL_JEV: jev.service(identity=IDENTITY_JEV, response_model=RESPONSE_JEV),
    }

    def factory(config: DecisionsConfig) -> ev.NoulReplayer:
        assert config.model is not None
        seen[config.model] = config
        return services[config.model]

    out = tmp_path / "report.md"
    ev.main(
        [
            "--consumer",
            TOOL,
            "--dataset",
            str(dataset),
            "--out",
            str(out),
            "--decisions",
            str(clef_yaml),
            "--decisions",
            str(jev_yaml),
        ],
        service_factory=factory,
        resolve_secret=resolve_secret,
        clock=clock,
    )
    text = out.read_text()
    assert seen[MODEL_CLEF].api_key == SECRET_VALUE
    assert SECRET_VALUE not in text
    assert IDENTITY_CLEF in text
    assert IDENTITY_JEV in text
    for quoted_cost in ("$0.042", "$0.24", "$0.09"):
        assert quoted_cost in text
    assert any("|" in line and "clef" in line and "jev" in line for line in text.splitlines())

    # With no --decisions, the daemon's ai.decisions is the one backend.
    daemon_out = tmp_path / "daemon.md"
    ev.main(
        ["--consumer", TOOL, "--dataset", str(dataset), "--out", str(daemon_out)],
        service_factory=factory,
        resolve_secret=resolve_secret,
        load_daemon_decisions=lambda: decisions_config(MODEL_JEV, "openrouter-decisions"),
        clock=clock,
    )
    daemon_text = daemon_out.read_text()
    assert MODEL_JEV in daemon_text
    assert MODEL_CLEF not in daemon_text


def test_cli_runs_as_standalone_script() -> None:
    # Direct execution puts scripts/ on sys.path, not the repo root, so the entry point must
    # import without the package prefix and still reach argument parsing.
    script = Path(__file__).resolve().parents[2] / "scripts" / "decisions_eval.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "--consumer" in result.stdout


def test_metrics_include_required_measures_at_selected_threshold() -> None:
    # Empty rows score 0.6 on candidate a. The dev split selects min_probability 0.65, the
    # lowest grid value that rejects every empty row while keeping gold. Accuracy at 0.65 is
    # 1.0; at the 0.5 default the 25 empty rows scoring 0.6 would count as wrong (0.75).
    clock = FakeClock()
    ds = Dataset(clock)
    build_tool(ds, DEV, empty_a=0.6)
    build_tool(ds, HOLDOUT, empty_a=0.6)
    report = ev.evaluate(TOOL, ds.records, [ds.backend("clef")], clock=clock)
    (backend,) = report.backends
    assert backend.thresholds["min_probability"] == pytest.approx(0.65)
    holdout = backend.metrics[HOLDOUT]
    assert holdout["accuracy"] == pytest.approx(1.0)
    assert holdout["p50_latency_ms"] == pytest.approx(100.0)
    assert holdout["unavailable_rate"] == pytest.approx(0.0)
    for key in (
        "brier",
        "ece",
        "order_flip_rate",
        "p95_latency_ms",
        "selective_accuracy@0.50",
        "selective_coverage@0.50",
        "reject_all_accuracy",
        "recall_at_k",
        "incumbent_recall_at_k",
        "paired_recall_at_k_delta",
    ):
        assert key in holdout, key


def test_found_metrics_include_accuracy_and_selective_measures() -> None:
    clock = FakeClock()
    ds = Dataset(clock)
    build_found(ds, DEV)
    build_found(ds, HOLDOUT)
    report = ev.evaluate(FOUND, ds.records, [ds.backend("clef")], clock=clock)
    holdout = report.backends[0].metrics[HOLDOUT]
    # Gold-shirk rows score 0.95 and gold-clean rows 0.05, so the confirm threshold is exact.
    assert holdout["accuracy"] == pytest.approx(1.0)
    for key in (
        "selective_accuracy@0.50",
        "selective_coverage@0.50",
        "false_alert_rate",
        "unavailable_rate",
        "p50_latency_ms",
    ):
        assert key in holdout, key


def _populate(consumer: str, ds: Dataset) -> None:
    for split in (DEV, HOLDOUT):
        if consumer == TOOL:
            build_tool(ds, split)
        else:
            build_found(ds, split)


SHARED_MEASURES = (
    "Accuracy",
    "Brier",
    "ECE",
    "Order-flip rate",
    "p50 latency ms",
    "p95 latency ms",
    "Unavailability rate",
)
CONSUMER_MEASURES = {
    TOOL: ("Reject-all accuracy", "Paired Recall@k delta", "Recall@k", "Incumbent Recall@k"),
    FOUND: (
        "False-clear rate",
        "Incumbent false-clear rate",
        "False-alert rate",
        "Escalation rate",
    ),
}


@pytest.mark.parametrize("consumer", [TOOL, FOUND])
def test_report_renders_measures_and_support_for_each_backend(consumer: str) -> None:
    clock = FakeClock()
    ds = Dataset(clock)
    _populate(consumer, ds)
    backends = [
        ds.backend("clef"),
        ds.backend(
            "jev",
            model=MODEL_JEV,
            wire_api="openrouter-decisions",
            identity=IDENTITY_JEV,
            response_model=RESPONSE_JEV,
        ),
    ]
    report = ev.evaluate(consumer, ds.records, backends, clock=clock)
    text = ev.render(report, "shadow.jsonl", "0" * 64)
    lines = text.splitlines()

    for label in ("clef", "jev"):
        assert f"## {label}" in text
    assert "| Measure | clef | jev |" in text
    assert f"| Measure | {DEV} | {HOLDOUT} |" in text
    assert "| Confidence threshold |" in text
    assert text.count("| Gate constant | Limit | Measured (holdout) |") == len(backends)
    for measure in SHARED_MEASURES + CONSUMER_MEASURES[consumer]:
        assert any(line.startswith(f"| {measure} |") for line in lines), measure
    positive, negative = (
        ("gold-relevant", "gold-empty") if consumer == TOOL else ("gold-shirk", "gold-clean")
    )
    assert f"{positive} 25" in text
    assert f"{negative} 25" in text


def test_reverse_call_latency_counts_toward_p95() -> None:
    # Forward calls take 100 ms. Reverse calls on 25 extra holdout records take 1.5 s. Those
    # 25 slow calls are 25 of the holdout's 150 calls, so p95 must read 1.5 s and fail the 1 s
    # gate. Timing only forward calls would report 100 ms and pass.
    clock = FakeClock()
    ds = Dataset(clock)
    build_tool(ds, DEV)
    build_tool(ds, HOLDOUT)
    for _ in range(25):
        ds.tool(
            HOLDOUT,
            gold=["a"],
            incumbent=["a", "b"],
            forward={"a": 0.95, "b": 0.05},
            reverse_latency_ms=1500.0,
        )
    report = ev.evaluate(TOOL, ds.records, [ds.backend("clef")], clock=clock)
    (backend,) = report.backends
    assert backend.metrics[HOLDOUT]["p95_latency_ms"] == pytest.approx(1500.0)
    assert backend.verdict == "FAIL"
    assert any("p95 latency" in reason for reason in backend.reasons)
