"""Authority, transport and replay checks for the role-owned task step record tools."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.step_records import current_task_steps, list_task_step_records
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit

_TIP = "871d706" + "a" * 33
_NEXT = "9c3ff62" + "b" * 33
_FEEDBACK = "Round 1 finding:\n- close skips the record gate.\n" + "detail " * 200
_KNOWN_COMMITS = (_TIP, _NEXT)
_TOOL_STEPS: tuple[tuple[str, str], ...] = (
    ("record_test_step", "test"),
    ("record_implementation_step", "implementation"),
    ("record_review_step", "review"),
    ("record_closing_step", "closing"),
)
_TOOLS = tuple(tool for tool, _ in _TOOL_STEPS)
# Seat name -> the one dedicated writer its installed definition grants.
_OWNED_TOOL = {
    "tester": "record_test_step",
    "implementer": "record_implementation_step",
    "reviewer": "record_review_step",
    "closer": "record_closing_step",
}
_ALLOWED_KEYS = {
    "task_id",
    "status",
    "epoch",
    "round_number",
    "feedback",
    "commit_sha",
    "disposition",
}
_REQUIRED_KEYS = {"task_id", "status", "epoch", "round_number", "feedback"}


def _full_sha(sha: str, cwd: str) -> str | None:
    """Stand in for git: a unique prefix of a linked test commit resolves to its full SHA."""
    if len(sha) < 4:
        return None
    matches = [known for known in _KNOWN_COMMITS if known.startswith(sha)]
    return matches[0] if len(matches) == 1 else None


def _args(task_id: str, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "task_id": task_id,
        "status": "approved",
        "epoch": 1,
        "round_number": 1,
        "feedback": _FEEDBACK,
        "commit_sha": _TIP[:10],
    }
    args.update(overrides)
    return args


class _Seats:
    """Registered sessions whose installed definitions grant task step writes."""

    def __init__(self, db: HubDatabase, project_id: str) -> None:
        self.db = db
        self.project_id = project_id
        self.next_seq = 2100

    def add(self, definition: str, writes: list[str]) -> str:
        definitions = AgentDefinitionManager(self.db)
        if definitions.get_by_name(definition, self.project_id) is None:
            definitions.create(
                definition,
                {
                    "name": definition,
                    "version": "1.0",
                    "prompts": {"agent": "Record the assigned task step."},
                    "workflows": {"rule_selectors": {"include": []}},
                    "task_step_writes": writes,
                },
                project_id=self.project_id,
            )
        session_id = str(uuid4())
        self.next_seq += 1
        self.db.execute(
            """
            INSERT INTO sessions (
                id, external_id, machine_id, source, project_id, title,
                status, agent_depth, seq_num
            )
            VALUES (%s, %s, %s, 'test', %s, %s, 'active', 0, %s)
            """,
            (
                session_id,
                f"ext-{session_id}",
                require_machine_id(),
                self.project_id,
                definition,
                self.next_seq,
            ),
        )
        SessionVariableManager(self.db).set_variable(session_id, "_agent_type", definition)
        return session_id


@pytest.fixture
def seats(temp_db: HubDatabase, sample_project: dict[str, Any]) -> _Seats:
    return _Seats(temp_db, sample_project["id"])


@pytest.fixture
def team(seats: _Seats) -> dict[str, str]:
    return {
        "tester": seats.add("step-tester", ["test"]),
        "implementer": seats.add("step-implementer", ["implementation"]),
        "reviewer": seats.add("step-reviewer", ["review"]),
        "closer": seats.add("step-closer", ["closing"]),
    }


@pytest.fixture
def manager(temp_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def task(manager: LocalTaskManager, sample_project: dict[str, Any], team: dict[str, str]) -> Task:
    created = manager.create_task(
        sample_project["id"],
        "Step-recorded task",
        validation_criteria="Independent review approves the exact task tip.",
    )
    manager.claim_task(created.id, team["implementer"])
    manager.link_commit(created.id, _TIP[:10])
    return manager.get_task(created.id)


@pytest.fixture
def registry(manager: LocalTaskManager) -> Iterator[InternalToolRegistry]:
    """The gobby-tasks registry with the repository boundary stubbed to the linked commits."""
    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._step_records.resolve_task_repo_path",
            return_value="/repo",
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._step_records.resolve_full_commit_sha",
            side_effect=_full_sha,
        ),
    ):
        yield create_task_registry(manager)


async def test_role_owned_writers(
    temp_db: HubDatabase,
    team: dict[str, str],
    task: Task,
    registry: InternalToolRegistry,
) -> None:
    with session_context_for_test(team["tester"]):
        started = await registry.call("record_test_step", _args(task.id, status="in_progress"))
        approved = await registry.call("record_test_step", _args(task.id))
    with session_context_for_test(team["implementer"]):
        implemented = await registry.call("record_implementation_step", _args(task.id))
    with session_context_for_test(team["reviewer"]):
        reviewed = await registry.call("record_review_step", _args(task.id, status="rejected"))
    with session_context_for_test(team["closer"]):
        closed = await registry.call("record_closing_step", _args(task.id))

    records = list_task_step_records(temp_db, task.id)
    stored = {record.id: record for record in records}
    for result in (started, approved, implemented, reviewed, closed):
        assert result["created"] is True
        assert stored[result["record"]["id"]].model_dump(mode="json") == result["record"]

    expected = [
        (started, "step-tester", team["tester"], "test", "in_progress"),
        (approved, "step-tester", team["tester"], "test", "approved"),
        (implemented, "step-implementer", team["implementer"], "implementation", "approved"),
        (reviewed, "step-reviewer", team["reviewer"], "review", "rejected"),
        (closed, "step-closer", team["closer"], "closing", "approved"),
    ]
    for result, definition, seat, step, status in expected:
        record = stored[result["record"]["id"]]
        assert (record.step, record.status, record.session_id, record.agent_definition) == (
            step,
            status,
            seat,
            definition,
        )
        assert record.agent_definition_version == "1.0"
        assert len(record.agent_definition_hash) == 64
        assert record.feedback == _FEEDBACK
        assert record.commit_sha == _TIP
        assert record.created_at <= record.updated_at

    # A later status for the same round appends and keeps the original creation time.
    assert stored[approved["record"]["id"]].created_at == stored[started["record"]["id"]].created_at

    projection = current_task_steps(records, 1)
    assert {step: record.session_id for step, record in projection.items()} == {
        "test": team["tester"],
        "implementation": team["implementer"],
        "review": team["reviewer"],
        "closing": team["closer"],
    }
    assert projection["test"].status == "approved"

    # Every other seat is refused for every step it does not own, and nothing is written.
    before = len(records)
    for seat, owned_tool in _OWNED_TOOL.items():
        for tool, step in _TOOL_STEPS:
            if tool == owned_tool:
                continue
            with session_context_for_test(team[seat]):
                refused = await registry.call(tool, _args(task.id))
            assert refused == {
                "error": f"agent definition step-{seat} cannot write the {step} step"
            }
    assert len(list_task_step_records(temp_db, task.id)) == before


async def test_review_only_grant_cannot_record_implementation(
    temp_db: HubDatabase,
    team: dict[str, str],
    task: Task,
    registry: InternalToolRegistry,
) -> None:
    with session_context_for_test(team["reviewer"]):
        result = await registry.call("record_implementation_step", _args(task.id))

    assert result == {
        "error": "agent definition step-reviewer cannot write the implementation step"
    }
    assert list_task_step_records(temp_db, task.id) == []


async def test_task_in_another_project_is_refused(
    temp_db: HubDatabase,
    tmp_path: Path,
    team: dict[str, str],
    registry: InternalToolRegistry,
) -> None:
    other = install_isolated_checkout_project(
        temp_db,
        tmp_path / "other-checkout",
        name="other-project",
        machine_id=require_machine_id(),
    )
    foreign = LocalTaskManager(temp_db).create_task(
        other.project.id,
        "Foreign task",
        validation_criteria="Only the owning project may record its steps.",
    )

    with session_context_for_test(team["tester"]):
        result = await registry.call("record_test_step", _args(foreign.id, status="in_progress"))

    assert result == {"error": "task_not_found"}
    assert list_task_step_records(temp_db, foreign.id) == []


async def test_definition_disabled_after_grant_is_refused(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    team: dict[str, str],
    task: Task,
    registry: InternalToolRegistry,
) -> None:
    definitions = AgentDefinitionManager(temp_db)
    grant = definitions.get_by_name("step-tester", sample_project["id"])
    assert grant is not None
    definitions.update(grant.id, enabled=False)

    with session_context_for_test(team["tester"]):
        result = await registry.call("record_test_step", _args(task.id, status="in_progress"))

    assert result == {"error": "step writer requires an enabled installed agent definition"}
    assert list_task_step_records(temp_db, task.id) == []


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize("key", ["step", "session_id", "agent_definition"])
async def test_unknown_argument_keys_are_refused(
    temp_db: HubDatabase,
    team: dict[str, str],
    task: Task,
    registry: InternalToolRegistry,
    tool: str,
    key: str,
) -> None:
    with session_context_for_test(team["tester"]), pytest.raises(ValueError, match="Unknown"):
        await registry.call(tool, {**_args(task.id, status="in_progress"), key: "test"})

    assert list_task_step_records(temp_db, task.id) == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_step_tool_schemas_are_closed(registry: InternalToolRegistry, tool: str) -> None:
    schema = registry.get_schema(tool)
    assert schema is not None
    input_schema = schema["inputSchema"]
    assert input_schema["additionalProperties"] is False
    assert set(input_schema["properties"]) == _ALLOWED_KEYS
    assert set(input_schema["required"]) == _REQUIRED_KEYS
    for number in ("epoch", "round_number"):
        assert input_schema["properties"][number]["type"] == "integer"
        assert input_schema["properties"][number]["minimum"] == 1
    assert input_schema["properties"]["status"]["enum"] == [
        "pending",
        "in_progress",
        "approved",
        "rejected",
    ]
    assert input_schema["properties"]["disposition"]["enum"] == [
        "moot",
        "superseded",
        "wont_do",
    ]


def test_review_description_states_rejection_rules(registry: InternalToolRegistry) -> None:
    schema = registry.get_schema("record_review_step")
    assert schema is not None
    description = schema["description"]
    for phrase in (
        "infrastructure",
        "pending",
        "in_progress",
        "never count",
        "judged rejection",
        "third",
        "escalat",
        "audited reset",
    ):
        assert phrase in description


async def test_exact_replay_returns_same_record(
    temp_db: HubDatabase,
    team: dict[str, str],
    task: Task,
    registry: InternalToolRegistry,
) -> None:
    with session_context_for_test(team["tester"]):
        first = await registry.call("record_test_step", _args(task.id, status="in_progress"))
        replay = await registry.call("record_test_step", _args(task.id, status="in_progress"))

    assert first["created"] is True
    assert replay["created"] is False
    assert replay["record"]["id"] == first["record"]["id"]
    assert replay["record"] == first["record"]
    assert len(list_task_step_records(temp_db, task.id)) == 1


async def test_unresolved_commit_ref_is_refused(
    temp_db: HubDatabase,
    team: dict[str, str],
    task: Task,
    registry: InternalToolRegistry,
) -> None:
    with session_context_for_test(team["tester"]):
        result = await registry.call(
            "record_test_step",
            _args(task.id, status="in_progress", commit_sha="deadbee"),
        )

    assert result == {"error": "Invalid or unresolved commit SHA: deadbee"}
    assert list_task_step_records(temp_db, task.id) == []


async def test_invalid_evidence_returns_structured_error(
    temp_db: HubDatabase,
    team: dict[str, str],
    task: Task,
    registry: InternalToolRegistry,
) -> None:
    with session_context_for_test(team["tester"]):
        blank = await registry.call(
            "record_test_step", _args(task.id, status="in_progress", feedback="   ")
        )
        mixed = await registry.call(
            "record_test_step", _args(task.id, status="approved", disposition="moot")
        )

    assert "feedback must describe the result" in blank["error"]
    assert "choose commit evidence or a no-work disposition" in mixed["error"]
    assert list_task_step_records(temp_db, task.id) == []


async def test_created_record_notifies_task_listeners(
    team: dict[str, str],
    task: Task,
    manager: LocalTaskManager,
    registry: InternalToolRegistry,
) -> None:
    listener = MagicMock()
    manager.add_change_listener(listener)

    with session_context_for_test(team["tester"]):
        first = await registry.call("record_test_step", _args(task.id, status="in_progress"))
        await registry.call("record_test_step", _args(task.id, status="in_progress"))

    assert first["created"] is True
    listener.assert_called_once_with()
