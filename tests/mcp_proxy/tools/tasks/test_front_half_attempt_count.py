"""Front-half attempt counters must read stage-state counters, not labels."""

from __future__ import annotations

import re

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def _front_half_source() -> str:
    return source_text("src/gobby/mcp_proxy/tools/tasks/_front_half.py")


def test_no_label_reads() -> None:
    source = _front_half_source()

    assert "PLANNING_ROUND_LABEL_PREFIX" not in source
    assert "planning-round:" not in source
    assert "qa-attempts:" not in source
    assert re.search(r"(?<!work_)attempt_count\b", source) is None


def test_planning_round_callers_read_review_round_count() -> None:
    source = _front_half_source()

    assert "planning_round" in source
    assert "review_round_count" in source


def test_qa_attempts_callers_read_review_round_count() -> None:
    source = _front_half_source()

    assert "qa-attempts:" not in source
    assert "review_round_count" in source
