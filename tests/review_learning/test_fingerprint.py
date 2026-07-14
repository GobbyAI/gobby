from __future__ import annotations

import pytest

from gobby.review_learning.fingerprint import (
    build_occurrence_key,
    derive_finding_fingerprint,
    fingerprint_tag,
    occurrence_tag,
)

pytestmark = pytest.mark.unit


def test_derived_fingerprint_is_line_agnostic() -> None:
    finding = {
        "rule_id": "sql.placeholder",
        "principle": "Use psycopg placeholders",
        "title": "Wrong placeholder style",
        "path": "src/gobby/storage/example.py",
        "symbol": "save",
        "start_line": 10,
    }
    moved = {**finding, "start_line": 99, "end_line": 101}

    assert derive_finding_fingerprint(finding) == derive_finding_fingerprint(moved)


def test_derived_fingerprint_preserves_field_labels_and_empty_slots() -> None:
    rule_fingerprint = derive_finding_fingerprint({"rule_id": "X"})
    principle_fingerprint = derive_finding_fingerprint({"principle": "X"})

    assert rule_fingerprint != principle_fingerprint


def test_fallback_fingerprint_is_mapping_order_agnostic() -> None:
    finding = {"details": {"expected": "write", "actual": "skip"}, "severity": "high"}
    reordered = {"severity": "high", "details": {"actual": "skip", "expected": "write"}}

    assert derive_finding_fingerprint(finding) == derive_finding_fingerprint(reordered)


def test_native_fingerprint_passthrough() -> None:
    assert derive_finding_fingerprint({"finding_fingerprint": "sarif-native"}) == "sarif-native"


def test_occurrence_key_and_tags_are_deterministic() -> None:
    key = build_occurrence_key("review-1", "finding-1")

    assert key == "8:review-19:finding-1"
    assert occurrence_tag(key) == occurrence_tag(key)
    assert fingerprint_tag("finding-1") == fingerprint_tag("finding-1")
    assert occurrence_tag(key).startswith("occurrence:")


def test_occurrence_key_is_delimiter_safe() -> None:
    assert build_occurrence_key("rev:1", "fp") != build_occurrence_key("rev", "1:fp")
