"""Validate the cross-provider turn-lifecycle characterization fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

FIXTURE = Path(__file__).parents[1] / "fixtures" / "provider_contracts" / "turn-lifecycle.json"
PINNED_VERSIONS = {
    "claude": "2.1.263",
    "codex": "0.153.2",
    "droid": "0.190.0",
    "grok": "1.0.13",
    "qwen": "0.23.0",
    "agy": "1.1.27",
}
REQUIRED_CASES = {
    "normal_answer",
    "prose_question",
    "structured_question",
    "permission_wait",
    "approval",
    "denial",
    "user_interruption",
    "non_user_failure",
    "replacement_prompt_race",
    "session_exit",
}


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_fixture_has_exact_provider_baseline_and_provenance(contract: dict[str, Any]) -> None:
    assert contract["schema_version"] == 1
    assert contract["capture_date"]
    providers = {entry["provider"]: entry for entry in contract["providers"]}
    assert set(providers) == set(PINNED_VERSIONS)
    for provider, version in PINNED_VERSIONS.items():
        entry = providers[provider]
        assert entry["version"] == version
        assert version in entry["official_source"]
        assert entry["capture_command"]
        assert set(entry["cases"]) == REQUIRED_CASES


def test_each_case_is_bounded_and_classified(contract: dict[str, Any]) -> None:
    valid_dispositions = {"completed", "ended_non_user", "user_interrupted", "unknown"}
    valid_waits = {None, "input", "approval"}
    for provider in contract["providers"]:
        for scenario, case in provider["cases"].items():
            assert case["raw_events"], (provider["provider"], scenario)
            assert case["evidence"], (provider["provider"], scenario)
            assert isinstance(case["required_absent"], list)
            expected = case["expected"]
            assert expected["disposition"] in valid_dispositions
            assert expected["wait_kind"] in valid_waits
            assert expected["status"] in {
                "active",
                "paused",
                "interrupted",
                "awaiting_input",
                "awaiting_approval",
            }
            assert expected["distinction"]


def test_positive_interrupt_negative_completion_and_structural_waits(
    contract: dict[str, Any],
) -> None:
    for provider in contract["providers"]:
        cases = provider["cases"]
        assert cases["user_interruption"]["expected"] == {
            "disposition": "user_interrupted",
            "wait_kind": None,
            "status": "interrupted",
            "distinction": "whole_turn_interruption",
        }
        assert cases["normal_answer"]["expected"]["disposition"] == "completed"
        assert cases["normal_answer"]["expected"]["status"] == "paused"
        assert cases["prose_question"]["expected"]["distinction"] == "prose_question"
        assert cases["structured_question"]["expected"]["wait_kind"] == "input"
        assert cases["permission_wait"]["expected"]["wait_kind"] == "approval"


def test_tool_cancellation_failure_exit_and_replacement_are_distinct(
    contract: dict[str, Any],
) -> None:
    for provider in contract["providers"]:
        cases = provider["cases"]
        assert cases["denial"]["expected"]["distinction"] in {
            "denied_resolution",
            "tool_local_cancellation",
        }
        assert cases["non_user_failure"]["expected"]["disposition"] == "ended_non_user"
        assert cases["session_exit"]["expected"]["distinction"] == "session_exit"
        assert cases["replacement_prompt_race"]["expected"]["stale_ignored"] is True
        assert cases["replacement_prompt_race"]["expected"]["status"] == "active"


def test_claude_and_agy_interrupt_captures_are_complete_without_stop(
    contract: dict[str, Any],
) -> None:
    providers = {entry["provider"]: entry for entry in contract["providers"]}
    for provider in ("claude", "agy"):
        interruption = providers[provider]["cases"]["user_interruption"]
        assert "Stop" in interruption["required_absent"]
        assert all("Stop" not in event for event in interruption["raw_events"])
