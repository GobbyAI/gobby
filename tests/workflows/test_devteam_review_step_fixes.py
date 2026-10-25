"""Folded DevTeam seat fixes from #24824: reviewer base drift, reviewer waits on a
background run, a developer that already holds its claim, the bare CANDIDATE form and
developer sandbox temp and process guidance.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.engine.seat_messages import _CANDIDATE_GRAMMAR
from tests.agents._yaml_helpers import flat
from tests.workflows.test_seat_step_loops import (
    OWNER_ID,
    REPLACEMENT_SHA,
    SESSION_ID,
    SHA,
    SeatRun,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "src/gobby/install/shared"
SKILLS = SHARED / "skills/gobby"
AGENTS = get_bundled_agents_path()
PROCEDURES = SKILLS / "references/agents/procedures"
# Every definition that holds a review grant takes the same base-drift and background-run rules.
REVIEW_DEFINITIONS = ("code-reviewer", "docs-reviewer", "research-reviewer")
# A bare CANDIDATE line: the grammar refuses it, so no source may teach it.
BARE_CANDIDATE = re.compile(r"(?<![=\w])CANDIDATE (?:TASK|TASK_TITLE|RUN|WT|COMMIT)=")


def _agent(name: str) -> dict[str, Any]:
    agent: dict[str, Any] = yaml.safe_load((AGENTS / f"{name}.yaml").read_text())
    return agent


def _step(name: str, step: str) -> dict[str, Any]:
    steps: list[dict[str, Any]] = _agent(name)["step_workflow"]["steps"]
    return next(entry for entry in steps if entry["name"] == step)


def _guidance(name: str) -> str:
    """A definition's agent prompt plus its seat procedure, flattened."""
    body = AgentDefinitionBody.model_validate(_agent(name))
    assert body.prompts is not None and body.prompts.agent, name
    text = body.prompts.agent
    procedure = body.workflows.variables.get("_seat_procedure")
    if procedure:
        text += "\n" + (SKILLS / procedure.split(":", 1)[1]).read_text()
    return flat(text)


# (a) Base drift: a conflicting merge with the integration head is a PROOF_REQUEST, never a BOUNCE.


@pytest.mark.parametrize("name", REVIEW_DEFINITIONS)
def test_reviewer_procedure_routes_base_drift_to_proof_request(name: str) -> None:
    procedure = PROCEDURES / f"{name}.md"
    text = flat(procedure.read_text())
    drift = next(
        (line for line in procedure.read_text().splitlines() if "base drift" in line),
        None,
    )
    assert drift is not None, f"{name}.md has no base-drift step"
    drift = flat(drift)
    assert "EVENT=PROOF_REQUEST" in drift
    assert "conflict" in drift
    assert re.search(r"replacement CANDIDATE", drift) is not None
    # Base drift must not count toward the three-rejection escalation.
    assert re.search(r"\b(?:never|not|no)\b[^.]*\bBOUNCE\b", drift) is not None
    assert "record_review_step" not in drift
    assert "EVENT=PROOF_REQUEST" in text


async def test_reviewer_proof_request_keeps_review_until_replacement(
    temp_db: HubDatabase,
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    assert run.step == "review"

    await run.send(
        f"EVENT=PROOF_REQUEST TASK={task.id} SHA={SHA} NOTE=merge-with-base-conflicts",
        OWNER_ID,
    )
    # No verdict was sent and no rejection recorded: the unit stays open in review.
    assert run.step == "review"
    assert run.vars["candidate_sha"] == SHA

    await run.intake(task, REPLACEMENT_SHA)
    assert run.step == "review"
    assert run.vars["candidate_sha"] == REPLACEMENT_SHA
    assert run.vars["verdict_ready"] is False


def test_developer_answers_base_drift_with_a_merge_and_replacement() -> None:
    text = _guidance("developer")
    # Only a base-drift request needs the merge and a replacement CANDIDATE with its new SHA.
    assert re.search(r"base-drift request[^.]*\bmerge\b[^.]*replacement CANDIDATE", text)


# (b) A reviewer waiting on a background test run may wait in-turn with Monitor.


async def test_review_step_admits_monitor_for_background_runs(temp_db: HubDatabase) -> None:
    assert "Monitor" in _step("code-reviewer", "review")["allowed_tools"]

    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    await run.intake(run.task())
    assert run.step == "review"
    monitor = {"command": "until ! kill -0 4242; do sleep 5; done", "description": "pytest"}
    assert await run.admits_native("Monitor", monitor)
    # Only the review step waits on a run in-turn; idle await ends turns on durable waits.
    assert "Monitor" not in _step("code-reviewer", "await")["allowed_tools"]


@pytest.mark.parametrize("name", REVIEW_DEFINITIONS)
def test_review_steps_admit_monitor_for_background_runs(name: str) -> None:
    review = _step(name, "review")
    assert "Monitor" in review["allowed_tools"]
    status = flat(review["status_message"])
    assert re.search(r"background test run is not a durable wait", status) is not None
    assert "Monitor" not in _step(name, "await")["allowed_tools"]


# (c) A developer launched with task_id already holds its claim.


def test_claim_step_text_covers_an_already_held_claim() -> None:
    status = flat(_step("developer", "claim")["status_message"])
    # The step must tell a developer that already holds its claim (launched with
    # task_id, no Dev Manager) to call claim_task now instead of waiting for a handover.
    assert re.search(r"already hold", status) is not None, status
    held = status[status.index("already hold") :]
    assert re.search(r"claim_task", held) is not None
    assert re.search(r"\bnow\b|\bimmediately\b|without waiting", held) is not None


async def test_held_claim_advances_from_claim_step(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "developer")
    # A claim_task attempt before the claim step never binds the task.
    assert not await run.admits("gobby-tasks", "claim_task", {"task_id": "#1"})
    await run.load(*run.vars["required_skills"])
    assert run.step == "claim"

    task = run.task(owner=SESSION_ID)
    ref = f"#{task.seq_num}"
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": ref},
        {"result": {"task_id": task.id, "ref": ref, "already_claimed": True}},
    )
    assert run.step == "route_skills"
    assert run.vars["assigned_task_id"] == task.id


# (d) The bare CANDIDATE form: refused at send with the canonical grammar, taught nowhere.


@pytest.fixture
def messaging(monkeypatch: pytest.MonkeyPatch) -> tuple[InternalToolRegistry, MagicMock]:
    sessions = MagicMock()
    sessions.get = MagicMock(
        side_effect=lambda ref: MagicMock(
            id=ref, project_id="p", parent_session_id=None, agent_depth=0, status="active"
        )
    )
    sessions.resolve_session_reference = MagicMock(side_effect=lambda ref, project_id=None: ref)
    sessions.is_ancestor = MagicMock(return_value=False)
    messages = MagicMock()
    monkeypatch.setattr(
        "gobby.workflows.engine.seat_messages.SessionManager", MagicMock(return_value=sessions)
    )
    monkeypatch.setattr("gobby.workflows.engine.seat_messages.LocalTaskManager", MagicMock())
    registry = InternalToolRegistry(name="gobby-agents", description="Candidate send")
    add_messaging_tools(
        registry=registry, message_manager=messages, session_manager=sessions, db=MagicMock()
    )
    return registry, messages


@pytest.mark.parametrize(
    "line",
    [
        f"CANDIDATE TASK_TITLE=Scroll RUN=red,green WT=tree COMMIT={SHA}",
        f"CANDIDATE TASK=#7 TASK_TITLE=Scroll RUN=run WT=tree COMMIT={SHA} NOTE=ready",
    ],
)
async def test_bare_candidate_is_refused_with_canonical_grammar(
    messaging: tuple[InternalToolRegistry, MagicMock], line: str
) -> None:
    registry, messages = messaging
    result = await registry.call(
        "send_message",
        {"from_session": "s-from", "target": "session", "target_id": "s-to", "content": line},
    )
    assert result["success"] is False, result
    assert result["candidate_binding_error"] == _CANDIDATE_GRAMMAR
    assert "EVENT=CANDIDATE TASK=" in _CANDIDATE_GRAMMAR
    messages.create_message.assert_not_called()


def test_every_documented_candidate_form_is_canonical() -> None:
    sources = [
        *AGENTS.glob("*.yaml"),
        *(SKILLS / "references").rglob("*.md"),
        *(SHARED / "workflows").rglob("*.yaml"),
        *(ROOT / "docs").rglob("*.md"),
    ]
    bare = {
        str(path.relative_to(ROOT)): found
        for path in sources
        if (found := BARE_CANDIDATE.findall(path.read_text()))
    }
    assert bare == {}
    # Every seat that sends, routes or launches with a CANDIDATE shows the canonical line.
    for seat in ("developer", "code-reviewer", "dev-manager", "orchestrator"):
        assert "EVENT=CANDIDATE TASK=" in _guidance(seat), seat


# (e) Developer seats write temp files under the run's TMPDIR and list processes by PID.


def test_developer_sandbox_guidance() -> None:
    text = _guidance("developer")
    assert "$TMPDIR" in text
    assert re.search(r"never[^.]*literal /tmp|not /tmp|never /tmp", text) is not None
    assert re.search(r"PID[- ]only", text) is not None
