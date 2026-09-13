"""Render scored frozen-cohort results without changing measurements."""

from __future__ import annotations

import json

from ask_scoring_retrieval import _list, _mapping


def _report_metric(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return "unknown" if value is None else str(value)


def _report_window(value: object) -> str:
    window = _mapping(value, name="retrieval window")
    return " / ".join(
        (
            str(window.get("supported", "unknown")),
            _report_metric(window.get("reciprocal_rank")),
            _report_metric(window.get("gold_span_recall")),
        )
    )


def _report_completeness(value: object) -> str:
    window = _mapping(value, name="retrieval window")
    observations = _list(window.get("query_completeness", []), name="query completeness")
    complete = sum(
        1
        for observation in observations
        if isinstance(observation, dict) and observation.get("complete") is True
    )
    return f"{complete}/{len(observations)} complete"


def _report_window_detail(value: object) -> str:
    window = _mapping(value, name="retrieval window")
    return "; ".join(
        (
            f"first_supporting_query={window.get('first_supporting_query')}",
            f"first_supporting_rank={window.get('first_supporting_rank')}",
            "recall@5/10/20="
            f"{_report_metric(window.get('recall_at_5'))} / "
            f"{_report_metric(window.get('recall_at_10'))} / "
            f"{_report_metric(window.get('recall_at_20'))}",
            f"citation_precision@10={_report_metric(window.get('citation_precision_at_10'))}",
            f"wrong_domain_collisions={window.get('wrong_domain_collisions', 'unknown')}",
        )
    )


def _report_query_details(value: object) -> list[str]:
    window = _mapping(value, name="retrieval window")
    observations = _list(window.get("query_completeness", []), name="query completeness")
    return [
        "  - "
        + "; ".join(
            f"{name}={json.dumps(observation.get(name), separators=(',', ':'))}"
            for name in ("invocation_id", "complete", "completeness", "continuation")
        )
        for observation in (
            _mapping(item, name="query completeness observation") for item in observations
        )
    ]


def _report_class_accuracy(value: object) -> str:
    metrics = _mapping(value, name="answer class metrics")
    return " / ".join(
        (
            _report_metric(metrics.get("accuracy")),
            _report_metric(metrics.get("classification_accuracy")),
        )
    )


def render_report(result: object) -> str:
    """Materialize the complete human-auditable cohort result from scored JSON."""
    scored = _mapping(result, name="scored result")
    comparison = _mapping(scored.get("retrieval_comparison"), name="retrieval comparison")
    answer_quality = _mapping(scored.get("answer_quality"), name="answer quality")
    rows = [
        _mapping(row, name="scored question")
        for row in _list(scored.get("questions"), name="scored questions")
    ]
    lines = [
        "# Native Ask frozen-cohort report",
        "",
        f"Retrieval conclusion: **{comparison['conclusion']}**.",
        "Answer quality is reported separately; the historical 6/14 value is retrieval-only.",
        "",
        "## Per-question retrieval and answer measurements",
        "",
        "Before/after cells are `supported / reciprocal rank / gold-span recall`.",
        "Class accuracy cells are `claim accuracy / classification accuracy`.",
        "",
        "| Q | Primary | Queries | Before | After | Completeness | Answer | Gold | Source | Citation | Exact | Expected class | Classification | Direct accuracy | Inferred accuracy | Unknown accuracy | Abstention |",
        "|---|---|---:|---|---|---|---|---:|---:|---:|---:|---|---|---|---|---|---|",
    ]
    for row in rows:
        retrieval = row.get("retrieval")
        answer = row.get("answer_score")
        values = ["UNSCORED"] * 15
        if isinstance(retrieval, dict) and isinstance(answer, dict):
            before = _mapping(retrieval.get("before"), name="before retrieval")
            after = _mapping(retrieval.get("after"), name="after retrieval")
            class_accuracy = _mapping(answer.get("accuracy_by_class"), name="class accuracy")
            values = [
                str(retrieval.get("query_count", "unknown")),
                _report_window(before),
                _report_window(after),
                _report_completeness(after),
                str(answer.get("answer_correct", "unknown")),
                _report_metric(answer.get("gold_key_coverage")),
                _report_metric(answer.get("source_supported_claim_precision")),
                _report_metric(answer.get("citation_integrity")),
                _report_metric(answer.get("exact_value_coverage")),
                str(answer.get("expected_class", "unknown")),
                str(answer.get("classification_correct", "unknown")),
                _report_class_accuracy(class_accuracy.get("direct")),
                _report_class_accuracy(class_accuracy.get("inferred")),
                _report_class_accuracy(class_accuracy.get("unknown")),
                str(answer.get("honest_abstention", "unknown")),
            ]
        lines.append(
            f"| {row['question_id']} | {row['disposition']} | " + " | ".join(values) + " |"
        )

    lines.extend(["", "## Per-query retrieval detail", ""])
    for row in rows:
        retrieval = row.get("retrieval")
        if not isinstance(retrieval, dict):
            lines.append(f"- {row['question_id']}: UNSCORED ({row['disposition']})")
            continue
        for window_name in ("before", "after"):
            window = _mapping(retrieval.get(window_name), name=f"{window_name} retrieval")
            lines.append(f"### {row['question_id']} {window_name}")
            lines.append("")
            lines.append(f"- {_report_window_detail(window)}")
            lines.append("- Query completeness and continuation:")
            lines.extend(_report_query_details(window) or ["  - none"])
            lines.append("")

    lines.extend(
        [
            "",
            "## Aggregate comparison",
            "",
            f"Historical supported: {comparison['historical_supported_count']}/14.",
            f"New first-query supported: {comparison['new_before_supported_count']}/14.",
            f"New after-follow-up supported: {comparison['new_after_supported_count']}/14.",
            f"Lost historical hits: {comparison['lost_historical_hits']}",
            f"Gained hits: {comparison['gained_hits']}",
            f"Delta after follow-ups vs historical: {comparison['delta_after_vs_historical']}",
            "",
            "Answer quality: "
            f"{answer_quality['correct_question_count']}/{answer_quality['scored_question_count']} "
            "correct; mean gold/source/citation = "
            f"{_report_metric(answer_quality['mean_gold_key_coverage'])} / "
            f"{_report_metric(answer_quality['mean_source_supported_claim_precision'])} / "
            f"{_report_metric(answer_quality['mean_citation_integrity'])}.",
            "",
            "## Runtime and usage",
            "",
            f"`{json.dumps(scored.get('runtime'), sort_keys=True, separators=(',', ':'))}`",
            "",
            "## Raw artifact inventory",
            "",
        ]
    )
    for row in rows:
        raw = row.get("raw")
        lines.append(
            f"- {row['question_id']}: `{json.dumps(raw, sort_keys=True, separators=(',', ':'))}`"
        )

    lines.extend(["", "## Unsupported statements and missing exact values", ""])
    for row in rows:
        answer = row.get("answer_score")
        if not isinstance(answer, dict):
            lines.append(f"- {row['question_id']}: UNSCORED ({row['disposition']})")
            continue
        unsupported = answer.get("unsupported_statements", [])
        checks = _mapping(answer.get("exact_checks", {}), name="exact checks")
        missing = [
            name
            for name, item in checks.items()
            if not isinstance(item, dict) or item.get("verified") is not True
        ]
        exact_detail = ""
        if checks:
            exact_detail = "; exact_checks=" + json.dumps(
                checks, sort_keys=True, separators=(",", ":")
            )
        if "q14_changed_paths_exact" in answer:
            exact_detail += "; changed_paths_exact=" + str(answer.get("q14_changed_paths_exact"))
        lines.append(
            f"- {row['question_id']}: unsupported="
            f"{json.dumps(unsupported, sort_keys=True, separators=(',', ':'))}; "
            f"missing_exact={json.dumps(missing, separators=(',', ':'))}"
            f"{exact_detail}; ambiguity_reason="
            f"{json.dumps(answer.get('ambiguity_reason'), separators=(',', ':'))}"
        )
    return "\n".join(lines) + "\n"
