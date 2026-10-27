"""Behavior tests for the review and observation seats.

Every seat here is synthetic: step graphs are built inline and carry only the transition or
admission under test. No test reads a bundled definition, so prompt wording, field values,
sync state and reference paths are not pinned.
"""

from __future__ import annotations

from typing import Any

import pytest

from gobby.agents.sandbox_network import definition_sandbox_config
from gobby.agents.sandbox_policy import allowed_domains
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id
from gobby.workflows.condition_helpers_sessions import session_refs_equal
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.workflows.test_seat_step_loops import COORDINATOR_ID, SESSION_ID, SHA, SeatRun

pytestmark = pytest.mark.unit

CALL = "mcp__gobby__call_tool"
EDIT = {"file_path": "src/gobby/example.py", "old_string": "a", "new_string": "b"}

REVIEWER_VARIABLES: dict[str, Any] = {
    "candidate_received": False,
    "verdict_ready": False,
    "candidate_task": None,
    "candidate_sha": None,
    "candidate_owner": None,
    "candidate_received_at": None,
    "required_skills": [],
    "fixing": False,
    "candidate_disposition": None,
}


def _protocol(tool: str) -> dict[str, str]:
    """Route one successful call through the engine's seat protocol handler."""
    return {"server": "gobby-agents", "tool": tool, "action": "advance_seat"}


def _seat(
    name: str,
    steps: list[dict[str, Any]],
    variables: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "prompts": {"agent": f"Synthetic {name} seat."},
        "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        "step_workflow": {"variables": dict(variables or {}), "steps": steps},
    }
    body.update(fields)
    return body


def _set_state(run: SeatRun, step: str, **variables: Any) -> None:
    """Place the seat in ``step`` with the given snapshot variables."""
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    instance.current_step = step
    instance.variables.update(variables)
    run.instances.save(instance)


# A synthetic claim-gated fixer. Only the claim hand-off transition is under test.
CLAIM_GATED_FIXER = _seat(
    "claim-fixer",
    [
        {
            "name": "await",
            "allowed_tools": [CALL],
            "allowed_mcp_tools": ["gobby-tasks:claim_task"],
            "on_mcp_success": [
                {
                    "server": "gobby-tasks",
                    "tool": "claim_task",
                    "action": "set_variable",
                    "variable": "fixing",
                    "value": True,
                }
            ],
            "transitions": [{"to": "fix", "when": "vars.fixing"}],
        },
        {"name": "fix", "allowed_tools": ["Edit", "Write"], "allowed_mcp_tools": []},
    ],
    {"fixing": False, "required_skills": []},
)

# A verdict seat: the protocol handler completes the unit, and the unit ends in await.
VERDICT_STEPS: list[dict[str, Any]] = [
    {
        "name": "verdict",
        "allowed_tools": [CALL],
        "on_mcp_success": [_protocol("send_message"), _protocol("get_inter_session_message")],
        "transitions": [{"to": "await", "when": "not vars.candidate_received"}],
    },
    {"name": "await", "allowed_tools": [CALL]},
]

# A minimal reviewer for the malformed-SHA case: only the verdict-completion transition.
MALFORMED_SHA_REVIEWER = _seat("code-reviewer", VERDICT_STEPS, REVIEWER_VARIABLES)

# A log monitor whose report step leaves for tick only on a durable report.
MONITOR = _seat(
    "log-monitor",
    [
        {
            "name": "report",
            "allowed_tools": [CALL],
            "on_mcp_success": [_protocol("send_message"), _protocol("get_inter_session_message")],
            "transitions": [{"to": "tick", "when": "not vars.tick_done"}],
        },
        {"name": "tick", "allowed_tools": [CALL]},
    ],
    {"tick_done": False, "tick_window": None},
)


def test_session_refs_equal_preserves_caller_project_scope(temp_db: PostgresHubDatabase) -> None:
    sessions = SessionManager(temp_db)
    projects = LocalProjectManager(temp_db)
    local_project = projects.create("metrics-local")
    foreign_project = projects.create("metrics-foreign")
    local, foreign = (
        sessions.register(
            external_id=f"metrics-scope-{project.name}",
            machine_id=get_machine_id(),
            source="codex",
            project_id=project.id,
        )
        for project in (local_project, foreign_project)
    )
    assert local.id != foreign.id
    assert local.seq_num == foreign.seq_num
    local_ref = f"#{local.seq_num}"
    foreign_ref = f"{foreign_project.name}#{foreign.seq_num}"
    assert session_refs_equal(sessions, local.id, local_ref, local.id)
    assert session_refs_equal(sessions, local.id, foreign_ref, foreign.id)
    assert not session_refs_equal(sessions, local.id, local_ref, foreign.id)
    assert not session_refs_equal(sessions, local.id, foreign_ref, local_ref)
    for invalid in (None, "", 1, "missing-project#99"):
        assert not session_refs_equal(sessions, local.id, invalid, invalid)
        assert not session_refs_equal(sessions, invalid, local.id, local.id)


def test_network_none_seat_keeps_loopback_for_the_sandbox() -> None:
    body = AgentDefinitionBody.model_validate(
        {
            "name": "offline-seat",
            "prompts": {"agent": "Synthetic offline seat."},
            "workflows": {"rule_selectors": {"include": [], "exclude": []}},
            "network": "none",
        }
    )
    sandbox = definition_sandbox_config(None, body)
    assert sandbox.enabled is True
    assert sandbox.allow_network is False
    assert {"localhost", "127.0.0.1"} <= set(allowed_domains(sandbox, "claude", None))


async def test_monitor_tick_admits_shell_and_report_does_not(temp_db: PostgresHubDatabase) -> None:
    steps = [
        {
            "name": "tick",
            "allowed_tools": [CALL, "Bash"],
            "allowed_mcp_tools": ["gobby-agents:wait_for_coordination"],
        },
        {
            "name": "report",
            "allowed_tools": [CALL],
            "allowed_mcp_tools": [
                "gobby-agents:send_message",
                "gobby-agents:wait_for_coordination",
            ],
        },
    ]
    run = SeatRun(temp_db, "log-monitor", definition=_seat("log-monitor", steps))
    assert await run.admits_native("Bash", {"command": "ls"})
    assert await run.admits("gobby-agents", "wait_for_coordination")
    _set_state(run, "report")
    assert not await run.admits_native("Bash", {"command": "ls"})
    assert await run.admits("gobby-agents", "send_message")


async def test_step_allowlist_refuses_an_unlisted_write_and_admits_a_listed_one(
    temp_db: PostgresHubDatabase,
) -> None:
    definition = _seat(
        "reviewer-scribe",
        [{"name": "write", "allowed_tools": ["Write", CALL]}],
    )
    run = SeatRun(temp_db, "reviewer-scribe", definition=definition)
    assert not await run.admits_native("Edit", EDIT)
    assert await run.admits_native("Write", EDIT)


async def test_load_step_refuses_serve_calls_and_serve_step_admits_its_handoff(
    temp_db: PostgresHubDatabase,
) -> None:
    steps = [
        {
            "name": "load_skills",
            "allowed_tools": [CALL],
            "allowed_mcp_tools": [
                "gobby-skills:get_skill",
                "gobby-skills:get_skill_file",
                "gobby-sessions:set_title",
            ],
            "transitions": [
                {
                    "to": "serve",
                    "when": "all(skill_loaded(skill) for skill in vars.required_skills)",
                }
            ],
        },
        {
            "name": "serve",
            "allowed_tools": [CALL],
            "allowed_mcp_tools": [
                "gobby-agents:send_message",
                "gobby-agents:wait_for_coordination",
                "gobby-agents:end_agent_run",
            ],
        },
    ]
    definition = _seat(
        "research-seat",
        steps,
        {"required_skills": ["gobby:references/synthetic/research.md"]},
    )
    run = SeatRun(temp_db, "research-seat", definition=definition)
    assert await run.admits("gobby-sessions", "set_title")
    assert not await run.admits("gobby-tasks", "claim_task")
    await run.load(*run.vars["required_skills"])
    assert run.step == "serve"
    assert await run.admits("gobby-agents", "end_agent_run")
    assert not await run.admits_native("Edit", EDIT)


async def test_edit_tools_admitted_only_after_a_claim_hands_the_seat_to_fix(
    temp_db: PostgresHubDatabase,
) -> None:
    run = SeatRun(temp_db, "claim-fixer", definition=CLAIM_GATED_FIXER)
    assert not await run.admits_native("Edit", EDIT)
    task = run.task()
    await run.call("gobby-tasks", "claim_task", {"task_id": task.id}, {"success": True})
    assert run.step == "fix"
    assert await run.admits_native("Edit", EDIT)
    assert await run.admits_native("Write", EDIT)


def _single_transition_seat(start: str, target: str, when: str) -> dict[str, Any]:
    """A two-step reviewer seat carrying exactly one transition."""
    steps = [
        {"name": start, "allowed_tools": [CALL], "transitions": [{"to": target, "when": when}]},
        {"name": target, "allowed_tools": [CALL]},
    ]
    return _seat("code-reviewer", steps, REVIEWER_VARIABLES)


@pytest.mark.parametrize(
    ("start", "target", "when", "state", "expected"),
    [
        pytest.param(
            "review",
            "await",
            "not vars.candidate_received",
            {"candidate_received": False, "verdict_ready": False},
            "await",
            id="stand-down-exits-without-a-verdict",
        ),
        pytest.param(
            "review",
            "verdict",
            "vars.verdict_ready",
            {"candidate_received": True, "verdict_ready": False},
            "review",
            id="unready-candidate-stays-in-review",
        ),
        pytest.param(
            "verdict",
            "review",
            "vars.candidate_received and not vars.verdict_ready",
            {"candidate_received": True, "verdict_ready": False},
            "review",
            id="verdict-returns-to-review-when-unready",
        ),
        pytest.param(
            "await",
            "verdict",
            "vars.candidate_received and vars.verdict_ready",
            {"candidate_received": True, "verdict_ready": True},
            "verdict",
            id="ready-candidate-goes-to-verdict",
        ),
    ],
)
async def test_reviewer_step_moves_only_on_its_own_transition(
    temp_db: PostgresHubDatabase,
    start: str,
    target: str,
    when: str,
    state: dict[str, Any],
    expected: str,
) -> None:
    run = SeatRun(
        temp_db,
        "code-reviewer",
        definition=_single_transition_seat(start, target, when),
    )
    _set_state(run, start, **state)
    await run.call(
        "gobby-agents",
        "send_message",
        {"target": "session", "target_id": COORDINATOR_ID, "content": "ping"},
    )
    assert run.step == expected


@pytest.mark.parametrize(
    ("content", "succeeded", "expected"),
    [
        pytest.param(
            "EVENT=CANDIDATE_WITHDRAWN TASK={task} SHA={sha} NOTE=already reviewed elsewhere",
            True,
            "await",
            id="exact-unit-delivered",
        ),
        pytest.param(
            "EVENT=CANDIDATE_WITHDRAWN TASK={task} SHA={sha} NOTE=already reviewed elsewhere",
            False,
            "verdict",
            id="exact-unit-failed-delivery",
        ),
        pytest.param(
            "EVENT=CANDIDATE_WITHDRAWN TASK=#99 NOTE=other candidate",
            True,
            "verdict",
            id="other-task",
        ),
        pytest.param(
            "EVENT=CANDIDATE_WITHDRAWN TASK={task} SHA={other} NOTE=other sha",
            True,
            "verdict",
            id="other-sha",
        ),
        pytest.param(
            "Prose EVENT=CANDIDATE_WITHDRAWN TASK={task} SHA={sha}",
            True,
            "verdict",
            id="prose-prefix",
        ),
        pytest.param(
            "EVENT=PROOF_REQUEST TASK={task} SHA={sha} NOTE=EVENT=CANDIDATE_WITHDRAWN",
            True,
            "verdict",
            id="proof-request-echo",
        ),
    ],
)
async def test_candidate_withdrawal_exits_only_for_its_own_unit(
    temp_db: PostgresHubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    succeeded: bool,
    expected: str,
) -> None:
    patch_local_machine_id(monkeypatch, "21000000-0000-4000-8000-000000000001")
    run = SeatRun(temp_db, "code-reviewer", definition=MALFORMED_SHA_REVIEWER)
    task = run.task()
    await run.intake(task)
    text = content.format(task=task.id, sha=SHA, other="b" * 40)
    message = InterSessionMessageManager(temp_db).create_message(COORDINATOR_ID, SESSION_ID, text)
    # A withdrawal is a stored incoming message the seat reads; a failed read must not clear.
    await run.call(
        "gobby-agents",
        "get_inter_session_message",
        {"message_id": message.id},
        {"success": succeeded, "message": message.to_dict()},
    )
    assert run.step == expected
    if expected == "await":
        assert run.vars["candidate_task"] is None
        assert run.vars["candidate_sha"] is None
        assert run.vars["candidate_received"] is False
        assert run.vars["verdict_ready"] is False


@pytest.mark.parametrize(
    ("content", "resets"),
    [
        ("EVENT=CANDIDATE_VERDICT TASK=#42 SHA=abc VERDICT=BOUNCE NOTE=HIGH", True),
        ("EVENT=CANDIDATE_VERDICT TASK=#42 SHA=abc VERDICT=LAND NOTE=approved", False),
        ("EVENT=CANDIDATE_VERDICT TASK=#43 SHA=abc VERDICT=LAND", False),
        (f"EVENT=CANDIDATE_VERDICT TASK=#42 SHA={'b' * 40} VERDICT=LAND", False),
        ("EVENT=LANDED TASK=#42 SHA=abc RECEIPT=r1 RETEST=NOT_REQUIRED", False),
        ("EVENT=ALREADY_LANDED TASK=#42 SHA=abc RECEIPT=r1 LANDED_BY_SESSION_ID=original", False),
        ("EVENT=REVIEW_COMPLETE TASK=#42 SHA=abc VERDICT=LAND LANDING=NOT_REQUIRED", False),
        ("Prose EVENT=CANDIDATE_VERDICT TASK=#42 SHA=abc VERDICT=LAND", False),
        ("EVENT=PROOF_REQUEST TASK=#42 SHA=abc NOTE=EVENT=CANDIDATE_VERDICT VERDICT=LAND", False),
    ],
    ids=[
        "bounce",
        "land-without-stored-approval",
        "other-task",
        "other-sha",
        "landed",
        "already-landed",
        "review-complete",
        "prose",
        "note",
    ],
)
async def test_candidate_verdict_resets_only_for_its_own_unit(
    temp_db: PostgresHubDatabase, content: str, resets: bool
) -> None:
    run = SeatRun(temp_db, "code-reviewer", definition=MALFORMED_SHA_REVIEWER)
    task = run.task()
    _set_state(
        run,
        "verdict",
        candidate_received=True,
        verdict_ready=True,
        candidate_task=task.id,
        candidate_sha=SHA,
    )
    before = run.vars
    await run.send(content.replace("TASK=#42", f"TASK={task.id}").replace("SHA=abc", f"SHA={SHA}"))
    assert run.step == ("await" if resets else "verdict")
    if resets:
        assert {
            key: run.vars[key]
            for key in ("candidate_received", "verdict_ready", "candidate_task", "candidate_sha")
        } == {
            "candidate_received": False,
            "verdict_ready": False,
            "candidate_task": None,
            "candidate_sha": None,
        }
    else:
        assert run.vars == before


@pytest.mark.parametrize("candidate_sha", ["", "abc", "g" * 40, None])
async def test_land_verdict_rejects_malformed_bound_sha(
    temp_db: PostgresHubDatabase, candidate_sha: str | None
) -> None:
    run = SeatRun(temp_db, "code-reviewer", definition=MALFORMED_SHA_REVIEWER)
    task = run.task()
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    # A corrupt recovered snapshot must never complete the unit.
    instance.current_step = "verdict"
    instance.variables.update(
        candidate_received=True,
        verdict_ready=True,
        candidate_task=task.id,
        candidate_sha=candidate_sha,
    )
    run.instances.save(instance)
    before = run.vars
    await run.send(f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={candidate_sha} VERDICT=LAND")
    assert run.step == "verdict"
    assert run.vars == before


@pytest.mark.parametrize(
    ("content", "resets"),
    [
        ("Systems nominal | window 14:00-14:10 | 3 warnings in 2 families, all mapped", True),
        ("EVENT=ALARM WINDOW=14:00-14:10 NOTE=hooks.log errors", True),
        ("Status: tick in progress for 14:00-14:10", False),
    ],
    ids=["nominal", "alarm", "status-reply"],
)
async def test_monitor_leaves_report_only_on_a_nominal_or_alarm_report(
    temp_db: PostgresHubDatabase, content: str, resets: bool
) -> None:
    run = SeatRun(temp_db, "log-monitor", definition=MONITOR)
    # Monitors report to Triage; only a durable report to Triage counts.
    SessionVariableManager(temp_db).set_variable(COORDINATOR_ID, "_agent_type", "triage")
    _set_state(run, "report", tick_done=True, tick_window="previous-window")
    await run.send(content)
    assert run.step == ("tick" if resets else "report")
    if resets:
        assert run.vars["tick_done"] is False
        assert run.vars["tick_window"] != "previous-window"
    else:
        assert run.vars["tick_window"] == "previous-window"
