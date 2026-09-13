"""Frozen gold criteria and deterministic retrieval overlap scoring.

Evaluation-only: never import into a corpus or include in Ask prompts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

HISTORICAL_RETRIEVAL_HITS = ("Q01", "Q07", "Q10", "Q11", "Q12", "Q13")


EXPECTED_ANSWER_CLASS = {
    **{f"Q{index:02d}": "direct" for index in range(1, 15)},
    "Q05": "inferred",
    "Q08": "unknown",
}


Q14_EXACT_CHECKS: dict[str, object] = {
    "automatic_non_excluded_scope": "required",
    "case_sensitive": True,
    "changed_path_count": 9,
    "hobby_supplies_minimum": 2,
    "longest_prefix_precedence": "required",
    "sleeves_prefix": "Sleeves: ",
    "sleeves_prefix_minimum": 4,
}


Q14_CHANGED_PATHS = (
    "config/replenishment.toml",
    "docs/replenishment.md",
    "src/game_goblins/platform/settings.py",
    "src/game_goblins/replenishment/daily.py",
    "src/game_goblins/replenishment/planner.py",
    "src/game_goblins/replenishment/store_targets.py",
    "tests/platform/test_settings.py",
    "tests/replenishment/test_planning_store.py",
    "tests/replenishment/test_store_targets.py",
)


GOLD_COMPONENTS = {
    "Q01": ("shared_platform_ownership", "restocks_standalone", "buylist_standalone", "cutover"),
    "Q02": ("lightspeed_authority", "postgres_mirror", "workflow_state"),
    "Q03": ("page_atomicity", "raw_and_normalized", "counts", "numeric_watermark", "all_resources"),
    "Q04": ("warehouse_little_rock", "shadow_review_files", "publish_preflight", "verify_publish"),
    "Q05": ("weekly_lane_order", "earlier_moves_stock", "floors_constrain_later_moves"),
    "Q06": (
        "shadow_default",
        "explicit_publish",
        "scope_preflight",
        "publish_lock",
        "readback",
        "application_state",
    ),
    "Q07": ("six_workbook_sheets", "transfer_publication_ready"),
    "Q08": (
        "committed_sync_survives",
        "failure_recorded",
        "bounded_reconciliation",
        "no_universal_resume",
        "partial_settings_action_ambiguous",
    ),
    "Q09": (
        "paged_products_inventory",
        "prior_day_sales",
        "three_report_types",
        "stock_cap",
        "slack_upload",
    ),
    "Q10": (
        "per_game_operational_csv",
        "unpriced_csv",
        "four_games",
        "google_sheets",
        "slack_status",
    ),
    "Q11": (
        "tcgcsv_daily_changes",
        "tcgplayer_hydration",
        "weekly_reconciliation",
        "retain_complete_snapshot",
    ),
    "Q12": ("buylist_not_shared", "standalone_operational", "integration_future"),
    "Q13": ("plans_and_history_intent", "code_tests_operational_truth", "mismatch_surfaced"),
    "Q14": (
        "nine_changed_paths",
        "name_prefix_rules",
        "case_sensitive",
        "longest_prefix",
        "automatic_non_excluded",
        "hobby_minimum_two",
        "sleeves_minimum_four",
    ),
}


@dataclass(frozen=True, slots=True)
class GoldSpan:
    path: str
    line_start: int
    line_end: int


_SPAN_DATA = """
D01 README.md:1:6;docs/architecture.md:3:95
D02 src/game_goblins/replenishment/sync_store.py:37:167;src/game_goblins/replenishment/sync_store.py:230:292
D03 src/game_goblins/replenishment/planner.py:251:498;src/game_goblins/replenishment/daily.py:273:381
D04 README.md:71:73;src/game_goblins/replenishment/weekly_writes.py:316:389;src/game_goblins/replenishment/weekly_writes.py:558:675
D05 src/game_goblins/replenishment/sync_store.py:230:292;src/game_goblins/replenishment/weekly_writes.py:769:853
D06 src/game_goblins/replenishment/vendor_workbook/writer.py:54:171;src/game_goblins/replenishment/weekly_writes.py:360:383
D07 Restocks/restockmaster.py:122:276;Restocks/restockmaster.py:324:420
D08 Buylist/README.md:1:23;Buylist/buylist_automation.py:426:456;Buylist/buylist_catalog.py:755:872
D09 README.md:38:48;docs/architecture.md:16:19;docs/architecture.md:49:55;docs/architecture.md:85:95
D10 src/game_goblins/platform/settings.py:197:214;src/game_goblins/replenishment/store_targets.py:41:75;config/replenishment.toml:83:95
"""


DOMAIN_SPANS: dict[str, tuple[GoldSpan, ...]] = {}
for _row in _SPAN_DATA.splitlines():
    if not _row:
        continue
    _domain, _encoded_spans = _row.split(" ", 1)
    DOMAIN_SPANS[_domain] = tuple(
        GoldSpan(_path, int(_start), int(_end))
        for _path, _start, _end in (item.rsplit(":", 2) for item in _encoded_spans.split(";"))
    )


QUESTION_DOMAINS = {
    key: tuple(value.split(","))
    for key, value in (
        item.split(":")
        for item in (
            "Q01:D01 Q02:D01 Q03:D02 Q04:D03,D04 Q05:D03 Q06:D04 Q07:D06,D04 "
            "Q08:D05 Q09:D07 Q10:D08 Q11:D08 Q12:D01,D08,D09 Q13:D09 Q14:D10"
        ).split()
    )
}


class ScoringError(RuntimeError):
    """External evidence or reviewed judgment is incomplete or inconsistent."""


def scoring_contract() -> dict[str, Any]:
    """Return the external scoring contract."""
    return {
        "schema_version": 1,
        "historical_retrieval_hits": list(HISTORICAL_RETRIEVAL_HITS),
        "expected_answer_class": dict(EXPECTED_ANSWER_CLASS),
        "exact_checks": {"Q14": dict(Q14_EXACT_CHECKS)},
        "gold_components": {key: list(value) for key, value in GOLD_COMPONENTS.items()},
        "q14_changed_paths": list(Q14_CHANGED_PATHS),
    }


def _mapping(value: object, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ScoringError(f"{name} must be a JSON object")
    return cast(dict[str, Any], value)


def _list(value: object, *, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ScoringError(f"{name} must be a JSON list")
    return value


def _source(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if item.get("item_type") == "source":
        return item
    if item.get("item_type") == "graph" and isinstance(item.get("source"), dict):
        return cast(dict[str, Any], item["source"])
    return None


def _overlaps(item: Mapping[str, Any], span: GoldSpan) -> bool:
    source = _source(item)
    if source is None or source.get("path") != span.path:
        return False
    start = source.get("line_start")
    end = source.get("line_end")
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and start <= span.line_end
        and end >= span.line_start
    )


def _gold_spans(question_id: str) -> tuple[GoldSpan, ...]:
    try:
        return tuple(
            span for domain in QUESTION_DOMAINS[question_id] for span in DOMAIN_SPANS[domain]
        )
    except KeyError as error:
        raise ScoringError(f"unknown question: {question_id}") from error


def _query_items(record: object) -> list[dict[str, Any]]:
    response = _mapping(_mapping(record, name="evidence record").get("response"), name="response")
    return [
        _mapping(item, name="evidence item") for item in _list(response.get("items"), name="items")
    ]


def _retrieval_window(question_id: str, records: Sequence[object]) -> dict[str, Any]:
    gold = _gold_spans(question_id)
    matches: list[tuple[int, int, dict[str, Any]]] = []
    hit_spans: set[int] = set()
    top_k_hits: dict[int, set[int]] = {5: set(), 10: set(), 20: set()}
    precision_hits = 0
    precision_total = 0
    wrong_domains = 0
    current_domains = set(QUESTION_DOMAINS[question_id])
    other_spans = tuple(
        span
        for domain, spans in DOMAIN_SPANS.items()
        if domain not in current_domains
        for span in spans
    )
    completeness: list[dict[str, object]] = []
    for query_index, raw_record in enumerate(records, start=1):
        record = _mapping(raw_record, name="evidence record")
        response = _mapping(record.get("response"), name="evidence response")
        items = _query_items(record)
        completeness.append(
            {
                "invocation_id": record.get("invocation_id"),
                "complete": response.get("complete"),
                "completeness": response.get("completeness"),
                "continuation": response.get("continuation"),
            }
        )
        for rank, item in enumerate(items, start=1):
            matched = {index for index, span in enumerate(gold) if _overlaps(item, span)}
            if matched:
                matches.append((query_index, rank, item))
                hit_spans.update(matched)
                for cutoff in top_k_hits:
                    if rank <= cutoff:
                        top_k_hits[cutoff].update(matched)
            elif any(_overlaps(item, span) for span in other_spans):
                wrong_domains += 1
            if rank <= 10 and _source(item) is not None:
                precision_total += 1
                precision_hits += bool(matched)
    first = min(matches, key=lambda value: (value[0], value[1])) if matches else None
    evidence_ids = sorted(
        {
            str(item.get("evidence_id"))
            for _query_index, _rank, item in matches
            if item.get("evidence_id") is not None
        }
    )
    return {
        "supported": bool(matches),
        "first_supporting_query": first[0] if first else None,
        "first_supporting_rank": first[1] if first else None,
        "reciprocal_rank": (1.0 / first[1]) if first else 0.0,
        "supporting_evidence_ids": evidence_ids,
        "gold_spans_hit": len(hit_spans),
        "gold_span_count": len(gold),
        "gold_span_recall": len(hit_spans) / len(gold),
        "recall_at_5": len(top_k_hits[5]) / len(gold),
        "recall_at_10": len(top_k_hits[10]) / len(gold),
        "recall_at_20": len(top_k_hits[20]) / len(gold),
        "citation_precision_at_10": (precision_hits / precision_total if precision_total else None),
        "wrong_domain_collisions": wrong_domains,
        "query_completeness": completeness,
    }


def score_retrieval(question_id: str, evidence_manifest: object) -> dict[str, Any]:
    """Score source-span overlap before and cumulatively after follow-up queries."""
    evidence = _mapping(evidence_manifest, name="evidence manifest")
    records = _list(evidence.get("records"), name="evidence records")
    return {
        "question_id": question_id,
        "query_count": len(records),
        "before": _retrieval_window(question_id, records[:1]),
        "after": _retrieval_window(question_id, records),
    }
