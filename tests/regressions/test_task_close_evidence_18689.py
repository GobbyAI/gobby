from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.mcp_proxy.tools.tasks._lifecycle_close import (
    CLOSE_VALIDATION_EVIDENCE_CONTEXT_LIMIT,
)
from gobby.mcp_proxy.tools.tasks._verification_evidence_context import (
    format_verification_evidence_context,
)
from gobby.sessions.transcripts.base import raw_lines_from_texts
from gobby.sessions.transcripts.codex import CodexNestedExecOutcome, CodexTranscriptParser
from gobby.workflows.condition_helpers import completion_evidence_ready
from gobby.workflows.observer_verification import detect_verification_evidence
from gobby.workflows.verification_evidence import append_verification_evidence

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "regressions" / "task_close_evidence_18689"
PROVIDER_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "provider_contracts" / "codex"
SESSION_ID = "scrubbed-session-9397"
EARLY_COMMAND = "GOBBY_TEST_PROTECT=1 uv run pytest tests/critical_early_regression.py -v"
SELECTOR_COMMAND = "GOBBY_TEST_PROTECT=1 uv run pytest -k critical_early_regression"


def _validation_item(
    command: str,
    success: bool | None,
    *,
    category: str = "test",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "evidence_type": "validation_command",
        "command": command,
        "success": success,
        "categories": [category],
    }
    if success is not None:
        item["exit_code"] = 0 if success else 1
        item["outcome_provenance"] = "tool_output.json.exit_code"
    return item


def _nested_outcomes() -> list[CodexNestedExecOutcome]:
    parser = CodexTranscriptParser()
    outcomes: list[CodexNestedExecOutcome] = []
    lines = (FIXTURE_ROOT / "raw_codex_events.jsonl").read_text().splitlines()
    for event in parser.iter_parse_events(raw_lines_from_texts(lines)):
        outcomes.extend(event.codex_exec_outcomes)
    return outcomes


def _codex_success_event(command: str) -> Any:
    payload = json.loads((PROVIDER_FIXTURE_ROOT / "command-execution-items.json").read_text())
    native = copy.deepcopy(
        next(record for record in payload["events"] if record["item"]["exitCode"] == 0)
    )
    native["item"]["command"] = command
    event = CodexAdapter().translate_to_hook_event({"method": native["type"], "params": native})
    assert event is not None
    return event


def test_more_than_fifty_commands_drop_relevant_early_results() -> None:
    retained: list[Any] = []
    commands = [
        EARLY_COMMAND,
        "uv run ruff check src/gobby/critical_early.py",
        *(f"uv run pytest tests/filler_{index:02d}.py" for index in range(50)),
    ]

    for command in commands:
        retained = append_verification_evidence(retained, _validation_item(command, True))

    assert len(commands) > 50
    assert len(retained) == 50
    retained_commands = {item["command"] for item in retained}
    assert EARLY_COMMAND not in retained_commands
    assert "uv run ruff check src/gobby/critical_early.py" not in retained_commands


def test_literal_functions_exec_exit_zero_does_not_reproduce_correlation_failure() -> None:
    findings = json.loads((FIXTURE_ROOT / "findings.json").read_text())

    outcomes = _nested_outcomes()

    assert [(item.command, item.result["exit_code"]) for item in outcomes] == [
        (
            "GOBBY_TEST_PROTECT=1 uv run pytest tests/scrubbed_regression.py -v",
            0,
        )
    ]
    hypothesis = findings["hypotheses"]["literal_functions_exec_correlation"]
    assert hypothesis == {
        "reproduced": False,
        "downstream_scope": False,
        "finding": "One literal nested exec_command with explicit exit_code 0 remains correlated.",
    }


def test_trusted_selector_success_is_weakened_to_unknown() -> None:
    event = _codex_success_event(SELECTOR_COMMAND)
    variables: dict[str, Any] = {}

    detect_verification_evidence(event, variables, SESSION_ID)

    evidence = variables["verification_evidence"][-1]
    assert event.data["tool_outcome"] == {
        "status": "succeeded",
        "exit_code": 0,
        "provenance": "event.exitCode",
    }
    assert evidence["evidence_requires_confirmation"] is True
    assert evidence["exit_code"] == 0
    assert evidence["success"] is None
    assert completion_evidence_ready(variables) is False


def test_identical_evidence_diverges_between_readiness_and_close_packet() -> None:
    over_budget_command = "uv run pytest " + ("over_catalog_budget_" * 500)
    evidence = [
        _validation_item(EARLY_COMMAND, True),
        *(
            _validation_item(f"pytest -k s{index:02d}", None)
            for index in range(1, CLOSE_VALIDATION_EVIDENCE_CONTEXT_LIMIT)
        ),
        _validation_item(over_budget_command, None),
    ]
    variables = {"verification_evidence": evidence}

    packet = format_verification_evidence_context(
        evidence,
        limit=CLOSE_VALIDATION_EVIDENCE_CONTEXT_LIMIT,
    )

    assert packet is not None
    packet_items = [json.loads(line) for line in packet.splitlines()[1:]]
    packet_commands = [item["command"] for item in packet_items if "command" in item]
    normalized_packet_commands = [
        "<over-catalog-budget-command>"
        if command.startswith("uv run pytest over_catalog_budget_")
        else command
        for command in packet_commands
    ]
    actual = {
        "source_fixture": "raw_codex_events.jsonl",
        "source_session": "#9397",
        "evidence_count": len(evidence),
        "readiness_ready": completion_evidence_ready(variables),
        "close_limit": CLOSE_VALIDATION_EVIDENCE_CONTEXT_LIMIT,
        "packet_result_count": len(packet_items),
        "packet_commands": normalized_packet_commands,
        "contains_early_success": EARLY_COMMAND in packet,
        "catalog_budget_truncated": len(packet_items) < CLOSE_VALIDATION_EVIDENCE_CONTEXT_LIMIT,
    }
    expected = json.loads((FIXTURE_ROOT / "assembled_close_packet.json").read_text())

    assert actual == expected
