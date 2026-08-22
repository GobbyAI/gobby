"""Oversized close-review agent-run contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks import agentic_close_review as review_module
from gobby.tasks.agentic_close_review import (
    TASK_CLOSE_VALIDATOR_AGENT,
    build_agentic_review_request,
    validate_agentic_review_run,
)

FINGERPRINT = "close-fingerprint"
EVIDENCE = "evidence-fingerprint"
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_agentic_review_request_is_taskless_and_fingerprinted() -> None:
    request = build_agentic_review_request(
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        close_fingerprint=FINGERPRINT,
        evidence_fingerprint=EVIDENCE,
    )

    spawn = cast(dict[str, object], request["spawn_request"])
    assert spawn["agent"] == TASK_CLOSE_VALIDATOR_AGENT
    assert spawn["task_id"] is None
    assert FINGERPRINT in str(spawn["prompt"])
    assert EVIDENCE in str(spawn["prompt"])


def test_task_close_validator_definition_is_read_only_and_taskless() -> None:
    path = REPO_ROOT / "src/gobby/install/shared/workflows/agents/task-close-validator.yaml"
    definition = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert definition["name"] == TASK_CLOSE_VALIDATOR_AGENT
    blocked = set(definition["blocked_mcp_tools"])
    assert "gobby-tasks:close_task" in blocked
    assert "gobby-tasks:update_task" in blocked
    assert "gobby-agents:spawn_agent" in blocked
    assert "taskless, read-only" in definition["prompts"]["agent"]
    assert "send_message" in definition["prompts"]["agent"]
    assert "zero-argument end_agent_run" in definition["prompts"]["agent"]


def test_completed_matching_agentic_review_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(result=json.dumps(_payload()))
    _install_run(monkeypatch, run)

    result = _validate()

    assert result.state == "ready"
    assert result.verdict == _payload()["verdict"]


@pytest.mark.parametrize(
    ("change", "error_type"),
    [
        ({"status": "pending"}, "agentic_review_pending"),
        ({"status": "error"}, "agentic_review_failed"),
        ({"agent_name": "other"}, "agentic_review_wrong_agent"),
        ({"result": "not-json"}, "agentic_review_malformed"),
        ({"prompt": "different"}, "agentic_review_stale"),
        ({"parent_session_id": "other"}, "agentic_review_wrong_parent"),
    ],
)
def test_agentic_review_fail_closed_states(
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, object],
    error_type: str,
) -> None:
    values: dict[str, object] = {
        "status": "success",
        "agent_name": TASK_CLOSE_VALIDATOR_AGENT,
        "task_id": None,
        "parent_session_id": "parent",
        "prompt": FINGERPRINT,
        "result": json.dumps(_payload()),
    }
    values.update(change)
    _install_run(monkeypatch, SimpleNamespace(**values))

    result = _validate()

    assert result.state != "ready"
    assert result.error_type == error_type


@pytest.mark.parametrize(
    "field",
    [
        "task_id",
        "commit_shas",
        "changes_summary",
        "close_fingerprint",
        "deterministic_evidence_fingerprint",
    ],
)
def test_agentic_review_rejects_stale_payload_field(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    payload = _payload()
    payload[field] = "stale"
    _install_run(monkeypatch, _run(result=json.dumps(payload)))

    result = _validate()

    assert result.error_type == "agentic_review_stale"


def test_agentic_review_requires_structured_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    payload["verdict"] = "valid"
    _install_run(monkeypatch, _run(result=json.dumps(payload)))

    result = _validate()

    assert result.error_type == "agentic_review_malformed"


def _payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "agent": TASK_CLOSE_VALIDATOR_AGENT,
        "task_id": "task",
        "commit_shas": ["abc"],
        "changes_summary": "summary",
        "close_fingerprint": FINGERPRINT,
        "deterministic_evidence_fingerprint": EVIDENCE,
        "verdict": {
            "status": "valid",
            "criteria": [{"index": 1, "satisfied": True, "gap": None}],
            "feedback": "valid",
        },
    }


def _run(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "status": "success",
        "agent_name": TASK_CLOSE_VALIDATOR_AGENT,
        "task_id": None,
        "parent_session_id": "parent",
        "prompt": FINGERPRINT,
        "result": json.dumps(_payload()),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _install_run(monkeypatch: pytest.MonkeyPatch, run: SimpleNamespace) -> None:
    manager = SimpleNamespace(get=lambda _run_id: run)
    monkeypatch.setattr(review_module, "LocalAgentRunManager", lambda _db: manager)


def _validate() -> review_module.AgenticReviewCheck:
    return validate_agentic_review_run(
        db=cast(HubDatabase, object()),
        review_run_id="run",
        parent_session_id="parent",
        task_id="task",
        commit_shas=["abc"],
        changes_summary="summary",
        close_fingerprint=FINGERPRINT,
        evidence_fingerprint=EVIDENCE,
    )
