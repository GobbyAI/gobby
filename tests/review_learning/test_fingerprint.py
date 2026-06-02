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


def test_native_fingerprint_passthrough() -> None:
    assert derive_finding_fingerprint({"finding_fingerprint": "sarif-native"}) == "sarif-native"


def test_occurrence_key_and_tags_are_deterministic() -> None:
    key = build_occurrence_key("review-1", "finding-1")

    assert key == "review-1:finding-1"
    assert occurrence_tag(key) == occurrence_tag(key)
    assert fingerprint_tag("finding-1") == fingerprint_tag("finding-1")
    assert occurrence_tag(key).startswith("occurrence:")
