from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.sessions.transcripts.base import raw_lines_from_texts
from gobby.sessions.transcripts.codex import CodexNestedExecOutcome, CodexTranscriptParser
from gobby.storage.verification_receipts import VerificationReceipt
from gobby.tasks.verification_receipt_packet import build_verification_receipt_packet
from gobby.utils.datetime import utc_now
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


def test_durable_packet_keeps_early_success_that_makes_readiness_true() -> None:
    over_budget_command = "uv run pytest " + ("over_catalog_budget_" * 500)
    evidence = [
        _validation_item(EARLY_COMMAND, True),
        *(_validation_item(f"pytest -k s{index:02d}", None) for index in range(1, 30)),
        _validation_item(over_budget_command, None),
    ]
    variables = {"verification_evidence": evidence}
    timestamp = utc_now()
    receipts = [
        VerificationReceipt(
            id=f"receipt-{index:03d}",
            project_id="project-1",
            session_id=SESSION_ID,
            task_id="task-1",
            provider="codex",
            execution_id=f"execution-{index:03d}",
            source_event_id=f"event-{index:03d}",
            evidence_type="validation_command",
            command=item["command"],
            cwd="/repo",
            normalized_outcome="success" if item["success"] is True else "unknown",
            outcome_provenance="tool_output.json.exit_code",
            exit_code=0 if item["success"] is True else None,
            started_at=timestamp + timedelta(seconds=index),
            completed_at=timestamp + timedelta(seconds=index),
            output_first_4k=None,
            output_last_4k=None,
            output_sha256=None,
            output_bytes=None,
            details={},
            attribution_source="sole_claim",
            attribution_actor=SESSION_ID,
            attributed_at=timestamp + timedelta(seconds=index),
            created_at=timestamp + timedelta(seconds=index),
            updated_at=timestamp + timedelta(seconds=index),
        )
        for index, item in enumerate(evidence)
    ]
    packet = build_verification_receipt_packet(receipts)

    assert completion_evidence_ready(variables) is True
    assert packet.error is None
    assert packet.text is not None
    assert EARLY_COMMAND in packet.text
    assert packet.disclosure.total == len(evidence)
    assert packet.disclosure.catalogued + packet.disclosure.aggregated == packet.disclosure.total
