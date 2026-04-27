from __future__ import annotations

import pytest

from gobby.plans.parser import PLAN_HEADING_REGEX

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("line", "section_id"),
    [
        ("### 1.1a", "1.1a"),
        ("### 1.1a Lifecycle enum and automation fields", "1.1a"),
        ("### 1.1d", "1.1d"),
        ("### 2.8a", "2.8a"),
        ("### 2.8b", "2.8b"),
        ("## A1", "A1"),
        ("## A1 Plan format spec (typed grammar)", "A1"),
        ("## A10", "A10"),
        ("### D0.1", "D0.1"),
        ("## B5", "B5"),
        ("## §1.7 Decision rules", "1.7"),
        ("### D0.8 Dispatcher slot reservation primitive (F11)", "D0.8"),
        ("### A1. Plan format spec (typed grammar)", "A1"),
    ],
)
def test_regex_pinned_strings(line: str, section_id: str) -> None:
    match = PLAN_HEADING_REGEX.match(line)

    assert match is not None
    assert match.group("section_id") == section_id


def test_negative_framing_heading() -> None:
    assert PLAN_HEADING_REGEX.match("## Phase A - Fix the Expansion/QA Contract") is None


def test_h1_not_subject_to_regex() -> None:
    assert PLAN_HEADING_REGEX.match("# Title") is None
