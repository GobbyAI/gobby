"""External scorer for native Ask cohort publications."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ask_cohort import (
    QUESTIONS,
    _sha256_file,
    _write_json_new,
    _write_new,
    aggregate_runtime,
    load_prepared_manifest,
    primary_accounting,
    read_verified_publication,
)

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


def _evidence_index(evidence_manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    records = _list(evidence_manifest.get("records"), name="evidence records")
    items: dict[str, Mapping[str, Any]] = {}
    for record_number, raw_record in enumerate(records, start=1):
        record = _mapping(raw_record, name=f"evidence record {record_number}")
        response = _mapping(record.get("response"), name="evidence response")
        for raw_item in _list(response.get("items"), name="evidence items"):
            item = _mapping(raw_item, name="evidence item")
            evidence_id = item.get("evidence_id")
            if not isinstance(evidence_id, str) or not evidence_id:
                raise ScoringError("evidence item has no non-empty evidence_id")
            if evidence_id in items and items[evidence_id] != item:
                raise ScoringError(f"conflicting evidence item: {evidence_id}")
            items[evidence_id] = item
    return items


def _fields_equal(
    actual: Mapping[str, Any], expected: Mapping[str, Any], fields: Sequence[str]
) -> bool:
    return all(actual.get(field) == expected.get(field) for field in fields)


def _citation_matches(
    citation: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    run_id: object,
) -> bool:
    if citation.get("run_id") != run_id:
        return False
    item_type = item.get("item_type")
    citation_type = citation.get("citation_type")
    if item_type == "source" and citation_type == "source":
        return _fields_equal(
            citation,
            item,
            (
                "evidence_id",
                "path",
                "blob_oid",
                "content_hash",
                "excerpt_hash",
                "qualified_name",
                "line_start",
                "line_end",
                "byte_start",
                "byte_end",
            ),
        )
    if item_type == "graph" and citation_type == "graph":
        source = _mapping(item.get("source"), name="graph evidence source")
        owner = _mapping(item.get("owner"), name="graph evidence owner")
        from_endpoint = _mapping(item.get("from"), name="graph from endpoint")
        to_endpoint = _mapping(item.get("to"), name="graph to endpoint")
        return (
            _fields_equal(
                citation,
                item,
                ("evidence_id", "relation", "direction", "provenance"),
            )
            and citation.get("source_evidence_id") == source.get("evidence_id")
            and citation.get("from_id") == from_endpoint.get("id")
            and citation.get("to_id") == to_endpoint.get("id")
            and citation.get("owner_path") == owner.get("path")
            and citation.get("owner_content_hash") == owner.get("content_hash")
            and citation.get("source_path") == source.get("path")
            and citation.get("line_start") == source.get("line_start")
            and citation.get("line_end") == source.get("line_end")
        )
    if item_type == "commit_metadata" and citation_type == "git_metadata":
        return _fields_equal(
            citation,
            item,
            (
                "evidence_id",
                "commit_oid",
                "parent_oids",
                "comparison_parent_oid",
                "comparison_kind",
                "changed_paths_digest",
                "changed_path_count",
                "changed_path",
                "record_hash",
            ),
        )
    return False


def citation_integrity(answer: object, evidence_manifest: object) -> dict[str, Any]:
    """Check every published citation against its full frozen evidence identity."""
    published = _mapping(answer, name="published answer")
    evidence = _mapping(evidence_manifest, name="evidence manifest")
    evidence_items = _evidence_index(evidence)
    claims = _list(published.get("claims"), name="published claims")
    invalid: list[dict[str, str]] = []
    uncited: list[str] = []
    total = 0
    valid = 0
    for raw_claim in claims:
        claim = _mapping(raw_claim, name="published claim")
        claim_id = claim.get("id")
        if not isinstance(claim_id, str) or not claim_id:
            raise ScoringError("published claim has no non-empty id")
        citations = _list(claim.get("citations"), name="claim citations")
        if claim.get("classification") in {"direct", "inferred"} and not citations:
            uncited.append(claim_id)
            invalid.append(
                {
                    "claim_id": claim_id,
                    "evidence_id": "",
                    "reason": "direct or inferred claim has no citation",
                }
            )
        for raw_citation in citations:
            citation = _mapping(raw_citation, name="claim citation")
            evidence_id = citation.get("evidence_id")
            total += 1
            item = evidence_items.get(evidence_id) if isinstance(evidence_id, str) else None
            if item is not None and _citation_matches(
                citation, item, run_id=evidence.get("run_id")
            ):
                valid += 1
            else:
                invalid.append(
                    {
                        "claim_id": claim_id,
                        "evidence_id": str(evidence_id),
                        "reason": "citation does not match a complete evidence identity",
                    }
                )
    return {
        "citation_count": total,
        "valid_citation_count": valid,
        "score": valid / (total + len(uncited)) if total or uncited else None,
        "uncited_claim_ids": uncited,
        "invalid_citations": invalid,
    }


def _required_bool(value: object, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ScoringError(f"{name} must be a boolean")
    return value


def _claim_ids(value: object, *, name: str, known: set[str]) -> list[str]:
    raw_ids = _list(value, name=name)
    claim_ids: list[str] = []
    for raw_id in raw_ids:
        if not isinstance(raw_id, str) or raw_id not in known:
            raise ScoringError(f"{name} contains an unknown claim id: {raw_id!r}")
        if raw_id in claim_ids:
            raise ScoringError(f"{name} repeats claim id: {raw_id}")
        claim_ids.append(raw_id)
    return claim_ids


def build_answer_score(
    question_id: str,
    answer: object,
    judgment: object,
    *,
    citation_integrity: float | None,
) -> dict[str, Any]:
    """Compute answer metrics only from an explicit, auditable reviewed judgment."""
    if question_id not in GOLD_COMPONENTS:
        raise ScoringError(f"unknown question id: {question_id}")
    published = _mapping(answer, name="published answer")
    review = _mapping(judgment, name="reviewed judgment")
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ScoringError("reviewed judgment requires a reviewer identity")

    claims: dict[str, Mapping[str, Any]] = {}
    for raw_claim in _list(published.get("claims"), name="published claims"):
        claim = _mapping(raw_claim, name="published claim")
        claim_id = claim.get("id")
        if not isinstance(claim_id, str) or not claim_id or claim_id in claims:
            raise ScoringError("published claim ids must be non-empty and unique")
        classification = claim.get("classification")
        if classification not in {"direct", "inferred", "unknown"}:
            raise ScoringError(f"invalid classification for claim {claim_id}")
        if not isinstance(claim.get("statement"), str):
            raise ScoringError(f"claim {claim_id} has no statement")
        claims[claim_id] = claim

    raw_reviews = _mapping(review.get("claim_reviews"), name="claim reviews")
    if set(raw_reviews) != set(claims):
        raise ScoringError("claim review keys must exactly match published claim ids")
    claim_reviews: dict[str, dict[str, Any]] = {}
    unsupported: list[dict[str, str]] = []
    class_counts = {
        name: {"claim_count": 0, "correct_count": 0, "classification_correct_count": 0}
        for name in ("direct", "inferred", "unknown")
    }
    for claim_id, reviewed_claim in claims.items():
        item = _mapping(raw_reviews[claim_id], name=f"review of claim {claim_id}")
        correct = _required_bool(item.get("correct"), name=f"{claim_id}.correct")
        supported = _required_bool(
            item.get("source_supported"), name=f"{claim_id}.source_supported"
        )
        class_correct = _required_bool(
            item.get("classification_correct"),
            name=f"{claim_id}.classification_correct",
        )
        notes = item.get("notes")
        if not isinstance(notes, str) or not notes.strip():
            raise ScoringError(f"review of claim {claim_id} requires notes")
        claim_reviews[claim_id] = {
            "correct": correct,
            "source_supported": supported,
            "classification_correct": class_correct,
            "notes": notes,
        }
        classification = cast(str, reviewed_claim["classification"])
        bucket = class_counts[classification]
        bucket["claim_count"] += 1
        bucket["correct_count"] += int(correct and supported)
        bucket["classification_correct_count"] += int(class_correct)
        if not supported:
            unsupported.append(
                {
                    "claim_id": claim_id,
                    "statement": cast(str, reviewed_claim["statement"]),
                    "reason": notes,
                }
            )

    raw_components = _mapping(review.get("gold_components"), name="gold components")
    expected_components = tuple(GOLD_COMPONENTS[question_id])
    if set(raw_components) != set(expected_components):
        raise ScoringError("gold component keys do not match the frozen scoring contract")
    component_results: dict[str, dict[str, Any]] = {}
    covered_count = 0
    for component in expected_components:
        item = _mapping(raw_components[component], name=f"gold component {component}")
        covered = _required_bool(item.get("covered"), name=f"{component}.covered")
        supporting_ids = _claim_ids(
            item.get("claim_ids"), name=f"{component}.claim_ids", known=set(claims)
        )
        if covered and not supporting_ids:
            raise ScoringError(f"covered component {component} requires supporting claims")
        verified = covered and all(
            claim_reviews[claim_id]["correct"] and claim_reviews[claim_id]["source_supported"]
            for claim_id in supporting_ids
        )
        covered_count += int(verified)
        component_results[component] = {
            "covered": covered,
            "verified": verified,
            "claim_ids": supporting_ids,
        }

    exact_results: dict[str, dict[str, Any]] = {}
    expected_checks = Q14_EXACT_CHECKS if question_id == "Q14" else {}
    raw_checks = _mapping(review.get("exact_checks"), name="exact checks")
    if set(raw_checks) != set(expected_checks):
        raise ScoringError("exact check keys do not match the frozen scoring contract")
    for check, expected in expected_checks.items():
        item = _mapping(raw_checks[check], name=f"exact check {check}")
        correct = _required_bool(item.get("correct"), name=f"{check}.correct")
        supporting_ids = _claim_ids(
            item.get("claim_ids"), name=f"{check}.claim_ids", known=set(claims)
        )
        notes = item.get("notes")
        if not isinstance(notes, str) or not notes.strip():
            raise ScoringError(f"exact check {check} requires notes")
        if correct and not supporting_ids:
            raise ScoringError(f"correct exact check {check} requires supporting claims")
        verified = correct and all(
            claim_reviews[claim_id]["correct"] and claim_reviews[claim_id]["source_supported"]
            for claim_id in supporting_ids
        )
        exact_results[check] = {
            "expected": expected,
            "correct": correct,
            "verified": verified,
            "claim_ids": supporting_ids,
            "notes": notes,
        }

    honest = _required_bool(review.get("honest_abstention"), name="honest_abstention")
    ambiguity_reason = review.get("ambiguity_reason")
    if ambiguity_reason is not None and not isinstance(ambiguity_reason, str):
        raise ScoringError("ambiguity_reason must be text or null")
    expected_class = EXPECTED_ANSWER_CLASS[question_id]
    classes = {cast(str, claim["classification"]) for claim in claims.values()}
    classification_correct = (
        all(item["classification_correct"] for item in claim_reviews.values())
        and expected_class in classes
    )
    if question_id == "Q08":
        classification_correct = (
            classification_correct
            and honest
            and isinstance(ambiguity_reason, str)
            and bool(ambiguity_reason.strip())
        )

    observed_paths = review.get("observed_changed_paths")
    paths_exact: bool | None = None
    if question_id == "Q14":
        paths = _list(observed_paths, name="observed changed paths")
        if not all(isinstance(path, str) for path in paths):
            raise ScoringError("observed changed paths must all be strings")
        paths_exact = tuple(paths) == Q14_CHANGED_PATHS
    elif observed_paths is not None:
        raise ScoringError("observed_changed_paths is only valid for Q14")

    claim_count = len(claims)
    supported_count = sum(int(item["source_supported"]) for item in claim_reviews.values())
    accuracy_by_class = {
        name: {
            **counts,
            "accuracy": (
                counts["correct_count"] / counts["claim_count"] if counts["claim_count"] else None
            ),
            "classification_accuracy": (
                counts["classification_correct_count"] / counts["claim_count"]
                if counts["claim_count"]
                else None
            ),
        }
        for name, counts in class_counts.items()
    }
    exact_verified = sum(int(item["verified"]) for item in exact_results.values())
    exact_coverage = exact_verified / len(exact_results) if exact_results else None
    if question_id == "Q14" and not paths_exact:
        exact_coverage = 0.0
    all_claims_verified = all(
        item["correct"] and item["source_supported"] and item["classification_correct"]
        for item in claim_reviews.values()
    )
    citations_required = any(
        claim["classification"] in {"direct", "inferred"} for claim in claims.values()
    )
    citations_valid = (
        citation_integrity == 1.0
        if citations_required
        else citation_integrity
        in {
            None,
            1.0,
        }
    )
    return {
        "question_id": question_id,
        "reviewer": reviewer,
        "answer_outcome": published.get("outcome"),
        "expected_class": expected_class,
        "classification_correct": classification_correct,
        "honest_abstention": honest,
        "ambiguity_reason": ambiguity_reason,
        "claim_count": claim_count,
        "correct_claim_count": sum(
            int(item["correct"] and item["source_supported"]) for item in claim_reviews.values()
        ),
        "source_supported_claim_precision": supported_count / claim_count if claim_count else None,
        "citation_integrity": citation_integrity,
        "citation_integrity_required": citations_required,
        "citation_integrity_passed": citations_valid,
        "all_claims_verified": all_claims_verified,
        "accuracy_by_class": accuracy_by_class,
        "gold_key_coverage": covered_count / len(expected_components),
        "gold_components": component_results,
        "unsupported_statements": unsupported,
        "exact_value_coverage": exact_coverage,
        "exact_checks": exact_results,
        "q14_changed_paths_exact": paths_exact,
        "answer_correct": (
            covered_count == len(expected_components)
            and all_claims_verified
            and classification_correct
            and citations_valid
            and (question_id != "Q14" or exact_coverage == 1.0)
        ),
    }


def _load_publication(
    attempt_dir: Path, export_value: object
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    export = _mapping(export_value, name="recorded publication export")
    raw_path = export.get("tar_path")
    if not isinstance(raw_path, str):
        raise ScoringError("recorded publication has no tar path")
    tar_path = Path(raw_path).resolve()
    if tar_path.parent.parent != attempt_dir.resolve():
        raise ScoringError("recorded publication escaped its primary attempt directory")
    recorded_files = _list(export.get("files"), name="recorded publication files")
    _manifest, answer, evidence, _files = read_verified_publication(
        tar_path,
        expected_tar_sha256=export.get("tar_sha256"),
        expected_files=recorded_files,
    )
    raw = {"tar_path": str(tar_path), "tar_sha256": export["tar_sha256"], "files": recorded_files}
    return answer, evidence, raw


def _supplement_inventory(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for kind in ("retry", "hybrid"):
        for attempt_dir in sorted(root.glob(f"attempts/Q*/{kind}-*")):
            outcome_path = attempt_dir / "outcome.json"
            attempt_path = attempt_dir / "attempt.json"
            source = outcome_path if outcome_path.is_file() else attempt_path
            body = _mapping(json.loads(source.read_bytes()), name="supplement account")
            rows.append(
                {
                    "question_id": body.get("question_id"),
                    "attempt_kind": body.get("attempt_kind"),
                    "attempt_index": body.get("attempt_index"),
                    "disposition": body.get("disposition", "interrupted"),
                    "path": str(attempt_dir),
                }
            )
    return rows


def _judgment_template(question_id: str, claim_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "reviewer": None,
        "gold_components": {
            component: {"covered": None, "claim_ids": []}
            for component in GOLD_COMPONENTS[question_id]
        },
        "claim_reviews": {
            claim_id: {
                "correct": None,
                "source_supported": None,
                "classification_correct": None,
                "notes": None,
            }
            for claim_id in claim_ids
        },
        "honest_abstention": None,
        "ambiguity_reason": None,
        "exact_checks": {
            check: {"correct": None, "claim_ids": [], "notes": None}
            for check in (Q14_EXACT_CHECKS if question_id == "Q14" else {})
        },
        "observed_changed_paths": [] if question_id == "Q14" else None,
    }


def build_review_packet(manifest_path: Path) -> dict[str, Any]:
    """Build an offline review packet from immutable primary publications."""
    manifest = load_prepared_manifest(manifest_path)
    accounts = primary_accounting(manifest_path)
    rows: list[dict[str, Any]] = []
    for account, (question_id, question, commit) in zip(accounts, QUESTIONS, strict=True):
        attempt_dir = manifest_path.parent / "attempts" / question_id / "primary"
        row: dict[str, Any] = {
            **account,
            "question": question,
            "source_commit": commit,
            "scorable": False,
            "answer": None,
            "evidence_manifest": None,
            "retrieval": None,
            "citation_integrity": None,
            "judgment_template": None,
            "raw": {
                "attempt": _raw_artifact(attempt_dir / "attempt.json"),
                "outcome": _raw_artifact(attempt_dir / "outcome.json"),
            },
        }
        if account.get("disposition") == "completed" and account.get("export") is not None:
            answer, evidence, raw = _load_publication(attempt_dir, account["export"])
            claims = _list(answer.get("claims"), name=f"{question_id} published claims")
            claim_ids = [
                cast(str, _mapping(claim, name="published claim")["id"]) for claim in claims
            ]
            row.update(
                {
                    "scorable": True,
                    "answer": answer,
                    "evidence_manifest": evidence,
                    "retrieval": score_retrieval(question_id, evidence),
                    "citation_integrity": citation_integrity(answer, evidence),
                    "judgment_template": _judgment_template(question_id, claim_ids),
                    "raw": {**cast(dict[str, Any], row["raw"]), "publication": raw},
                }
            )
        rows.append(row)
    attempted = sum(row["disposition"] != "unrun" for row in rows)
    state = "UNRUN" if attempted == 0 else ("PRIMARY_ACCOUNTED" if attempted == 14 else "PARTIAL")
    return {
        "schema_version": 1,
        "execution_state": state,
        "prepared_manifest": str(manifest_path.resolve()),
        "prepared_manifest_sha256": manifest_path.with_suffix(".sha256")
        .read_text(encoding="ascii")
        .strip(),
        "runtime_identity": manifest["runtime_identity"],
        "historical_retrieval_hits": list(HISTORICAL_RETRIEVAL_HITS),
        "questions": rows,
        "supplements": _supplement_inventory(manifest_path.parent),
        "note": "Only completed primary publications are scorable; supplements never substitute.",
    }


def _raw_artifact(path: Path) -> dict[str, str | None]:
    return {"path": str(path), "sha256": _sha256_file(path) if path.is_file() else None}


def score_packet(packet: object, judgments: object) -> dict[str, Any]:
    """Apply reviewed judgments while keeping retry data out of primary metrics."""
    review_packet = _mapping(packet, name="review packet")
    rows = [
        _mapping(row, name="review packet question")
        for row in _list(review_packet.get("questions"), name="review packet questions")
    ]
    expected_ids = [question_id for question_id, _question, _commit in QUESTIONS]
    observed_ids = [row.get("question_id") for row in rows]
    if observed_ids != expected_ids:
        raise ScoringError("review packet must contain the frozen 14 questions in order")
    judgment_file = _mapping(judgments, name="judgment file")
    raw_judgments = _mapping(judgment_file.get("judgments"), name="judgments")
    scorable_ids = {cast(str, row["question_id"]) for row in rows if row.get("scorable") is True}
    if set(raw_judgments) != scorable_ids:
        raise ScoringError("judgments must exactly match scorable primary questions")

    question_scores: list[dict[str, Any]] = []
    before_hits: set[str] = set()
    after_hits: set[str] = set()
    for row in rows:
        question_id = cast(str, row["question_id"])
        if row.get("scorable") is not True:
            question_scores.append(
                {
                    "question_id": question_id,
                    "scorable": False,
                    "disposition": row.get("disposition"),
                    "answer_score": None,
                    "retrieval": None,
                    "raw": row.get("raw"),
                }
            )
            continue
        retrieval = _mapping(row.get("retrieval"), name=f"{question_id} retrieval")
        before = _mapping(retrieval.get("before"), name=f"{question_id} before retrieval")
        after = _mapping(retrieval.get("after"), name=f"{question_id} after retrieval")
        if before.get("supported") is True:
            before_hits.add(question_id)
        if after.get("supported") is True:
            after_hits.add(question_id)
        integrity = _mapping(
            row.get("citation_integrity"), name=f"{question_id} citation integrity"
        )
        integrity_score = integrity.get("score")
        if integrity_score is not None and not isinstance(integrity_score, (int, float)):
            raise ScoringError(f"{question_id} citation integrity score is invalid")
        answer_score = build_answer_score(
            question_id,
            row.get("answer"),
            raw_judgments[question_id],
            citation_integrity=float(integrity_score) if integrity_score is not None else None,
        )
        question_scores.append(
            {
                "question_id": question_id,
                "scorable": True,
                "disposition": row.get("disposition"),
                "answer_score": answer_score,
                "retrieval": retrieval,
                "raw": row.get("raw"),
            }
        )

    historical = set(HISTORICAL_RETRIEVAL_HITS)
    complete = len(scorable_ids) == len(expected_ids)
    lost = sorted((historical & scorable_ids) - after_hits)
    gained = sorted(after_hits - historical)
    delta: int | None
    if complete:
        observed_delta = len(after_hits) - len(historical)
        delta = observed_delta
        if observed_delta > 0:
            conclusion = "improved"
        elif observed_delta < 0:
            conclusion = "regressed"
        else:
            conclusion = "unchanged"
    else:
        delta = None
        conclusion = f"blocked: {len(scorable_ids)}/14 primary answers are scorable"

    scored_answers = [
        cast(dict[str, Any], row["answer_score"])
        for row in question_scores
        if row["answer_score"] is not None
    ]
    answer_correct = sum(int(score["answer_correct"]) for score in scored_answers)
    gold_coverage = [float(score["gold_key_coverage"]) for score in scored_answers]
    source_precision = [
        float(value)
        for score in scored_answers
        if isinstance((value := score["source_supported_claim_precision"]), (int, float))
    ]
    citation_scores = [
        float(value)
        for score in scored_answers
        if isinstance((value := score["citation_integrity"]), (int, float))
    ]
    return {
        "schema_version": 1,
        "primary_question_count": len(rows),
        "scorable_primary_count": len(scorable_ids),
        "all_primary_scorable": complete,
        "supplements": review_packet.get("supplements", []),
        "supplements_excluded_from_primary_scoring": True,
        "retrieval_comparison": {
            "historical_supported_questions": list(HISTORICAL_RETRIEVAL_HITS),
            "historical_supported_count": len(historical),
            "new_before_supported_questions": sorted(before_hits),
            "new_before_supported_count": len(before_hits),
            "new_after_supported_questions": sorted(after_hits),
            "new_after_supported_count": len(after_hits),
            "lost_historical_hits": lost,
            "gained_hits": gained,
            "unscored_historical_hits": sorted(historical - scorable_ids),
            "delta_after_vs_historical": delta,
            "conclusion": conclusion,
        },
        "answer_quality": {
            "scored_question_count": len(scored_answers),
            "correct_question_count": answer_correct,
            "mean_gold_key_coverage": (statistics.fmean(gold_coverage) if gold_coverage else None),
            "mean_source_supported_claim_precision": (
                statistics.fmean(source_precision) if source_precision else None
            ),
            "mean_citation_integrity": (
                statistics.fmean(citation_scores) if citation_scores else None
            ),
            "note": "Answer quality is separate from the historical 6/14 retrieval baseline.",
        },
        "runtime": aggregate_runtime(rows),
        "questions": question_scores,
    }


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    review = actions.add_parser("review")
    review.add_argument("--manifest", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    score = actions.add_parser("score")
    score.add_argument("--packet", type=Path, required=True)
    score.add_argument("--judgments", type=Path, required=True)
    score.add_argument("--output-json", type=Path, required=True)
    score.add_argument("--output-report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "review":
            _write_json_new(args.output, build_review_packet(args.manifest))
        else:
            packet = json.loads(args.packet.read_bytes())
            judgments = json.loads(args.judgments.read_bytes())
            result = score_packet(packet, judgments)
            _write_json_new(args.output_json, result)
            _write_new(args.output_report, render_report(result).encode())
    except Exception as error:
        print(f"ask_scoring: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
