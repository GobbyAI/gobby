"""PostgreSQL tests for daemon-attested task close receipts."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.agentic_close_review import TASK_CLOSE_REVIEWER_AGENT, build_agentic_review_prompt
from gobby.tasks.close_receipts import (
    ACTIVATION,
    CALLER_RECEIPT_KINDS,
    CLOSE_RECEIPT_AUTHOR_TYPE,
    INDEPENDENT_REVIEW_APPROVAL,
    LANDING,
    LANDING_APPROVAL,
    CloseReceipt,
    CloseReceiptError,
    close_receipt_facts,
    list_close_receipts,
    record_close_receipt,
)
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

_TIP = "871d706" + "a" * 33
_LANDED = "fedfd114b6e31103849ccfc8c1382597835a2d11"
_PROMOTION_FACTS: dict[str, str | int | bool] = {
    "daemon_pid": 26253,
    "health": "ok",
    "schema": "v455",
    "binary": "gdaemon",
    "binary_version": "0.5.0",
    "binary_sha256": "c" * 64,
    "binary_inode": 91827364,
    "binary_signature": "adhoc",
    "promoted": True,
}


class _Sessions:
    def __init__(self, db: HubDatabase, project_id: str) -> None:
        self.db = db
        self.project_id = project_id
        self.next_seq = 900

    def add(self, title: str) -> str:
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
                title,
                self.next_seq,
            ),
        )
        return session_id


@pytest.fixture
def sessions(temp_db: HubDatabase, sample_project: dict[str, Any]) -> _Sessions:
    return _Sessions(temp_db, sample_project["id"])


@pytest.fixture
def roles(sessions: _Sessions) -> dict[str, str]:
    return {
        "creator": sessions.add("Program Director"),
        "claimant": sessions.add("Lane seat"),
        "reviewer": sessions.add("Independent reviewer"),
        "bystander": sessions.add("Other lane"),
    }


@pytest.fixture
def task(temp_db: HubDatabase, sample_project: dict[str, Any], roles: dict[str, str]) -> Task:
    manager = LocalTaskManager(temp_db)
    created = manager.create_task(
        sample_project["id"],
        "Receipted task",
        validation_criteria="Peer evidence reaches the close reviewer.",
        created_in_session_id=roles["creator"],
        claimed_by_session_id=roles["claimant"],
    )
    temp_db.execute(
        "UPDATE tasks SET delegated_by_session_id = %s WHERE id = %s",
        (roles["creator"], created.id),
    )
    return manager.get_task(created.id)


def _record(
    db: HubDatabase,
    task: Task,
    author: str,
    *,
    kind: str = INDEPENDENT_REVIEW_APPROVAL,
    sha: str = _TIP,
    facts: object = None,
) -> tuple[CloseReceipt, bool]:
    return record_close_receipt(
        db, task=task, author_session_id=author, kind=kind, commit_sha=sha, facts=facts
    )


def _seq_ref(db: HubDatabase, session_id: str) -> str:
    row = db.fetchone("SELECT seq_num FROM sessions WHERE id = %s", (session_id,))
    assert row is not None
    return f"#{row['seq_num']}"


def test_independent_approval_is_recorded_once_per_author_kind_and_commit(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    first, created = _record(temp_db, task, roles["reviewer"], sha=_TIP.upper())
    replay, replay_created = _record(temp_db, task, roles["reviewer"], facts={"note": "again"})

    assert created is True
    assert replay_created is False
    assert replay == first
    assert first.commit_sha == _TIP
    assert first.author_session_id == roles["reviewer"]
    row = temp_db.fetchone("SELECT author_type, body FROM task_comments WHERE id = %s", (first.id,))
    assert row is not None
    assert row["author_type"] == CLOSE_RECEIPT_AUTHOR_TYPE
    assert json.loads(row["body"]) == {
        "kind": INDEPENDENT_REVIEW_APPROVAL,
        "commit_sha": _TIP,
        "facts": {},
        "verdict": "LAND",
    }
    assert [receipt.id for receipt in list_close_receipts(temp_db, task.id)] == [first.id]


def test_claimant_cannot_attest_its_own_close(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    with pytest.raises(CloseReceiptError, match="claimant"):
        _record(temp_db, task, roles["claimant"])
    assert list_close_receipts(temp_db, task.id) == []


@pytest.mark.parametrize("kind", [INDEPENDENT_REVIEW_APPROVAL, LANDING_APPROVAL, LANDING])
def test_task_close_reviewer_session_cannot_record_receipts(
    temp_db: HubDatabase, task: Task, roles: dict[str, str], sessions: _Sessions, kind: str
) -> None:
    reviewer_session = sessions.add("Close reviewer")
    temp_db.execute(
        """
        INSERT INTO agent_runs (
            id, parent_session_id, child_session_id, machine_id, status, provider,
            prompt, agent_name
        )
        VALUES (%s, %s, %s, %s, 'running', 'codex', 'review', %s)
        """,
        (
            str(uuid4()),
            roles["claimant"],
            reviewer_session,
            require_machine_id(),
            TASK_CLOSE_REVIEWER_AGENT,
        ),
    )
    temp_db.execute(
        "UPDATE tasks SET delegated_by_session_id = %s WHERE id = %s",
        (reviewer_session, task.id),
    )

    with pytest.raises(CloseReceiptError, match="task-close reviewer"):
        _record(temp_db, task, reviewer_session, kind=kind, facts={"reason": "restart"})
    assert list_close_receipts(temp_db, task.id) == []


def test_landing_approval_comes_only_from_task_creator_or_delegator(
    temp_db: HubDatabase, task: Task, roles: dict[str, str], sessions: _Sessions
) -> None:
    delegator = sessions.add("Delegator")
    temp_db.execute(
        "UPDATE tasks SET delegated_by_session_id = %s WHERE id = %s", (delegator, task.id)
    )
    # Pass the old task snapshot: authority must come from the locked row.
    creator_receipt, creator_created = _record(
        temp_db, task, roles["creator"], kind=LANDING_APPROVAL, facts={"reason": "restart"}
    )
    delegator_receipt, delegator_created = _record(
        temp_db, task, delegator, kind=LANDING_APPROVAL, facts={"reason": "restart"}
    )
    assert creator_created and delegator_created
    assert creator_receipt.author_session_id == roles["creator"]
    assert delegator_receipt.author_session_id == delegator
    for role in ("claimant", "reviewer", "bystander"):
        with pytest.raises(CloseReceiptError, match="claimant|creator or delegator"):
            _record(temp_db, task, roles[role], kind=LANDING_APPROVAL, facts={"reason": "restart"})
    assert list_close_receipts(temp_db, task.id) == [creator_receipt, delegator_receipt]


@pytest.mark.parametrize(
    "facts",
    [
        None,
        {},
        {"reason": True},
        {"reason": 1},
        {"reason": ""},
        {"reason": " "},
        {"reason": "restart,unknown"},
        {"reason": "restart,"},
        {"reason": "freeze,,overlap"},
    ],
)
def test_landing_approval_requires_known_reasons(
    temp_db: HubDatabase, task: Task, roles: dict[str, str], facts: object
) -> None:
    with pytest.raises(CloseReceiptError, match="landing_approval.*reason"):
        _record(temp_db, task, roles["creator"], kind=LANDING_APPROVAL, facts=facts)
    assert list_close_receipts(temp_db, task.id) == []
    accepted_facts = {"reason": "restart,overlap", "batch": "landing-1"}
    receipt, created = _record(
        temp_db, task, roles["creator"], kind=LANDING_APPROVAL, facts=accepted_facts
    )
    assert created is True
    assert receipt.facts == accepted_facts
    assert list_close_receipts(temp_db, task.id) == [receipt]


async def test_tool_refuses_daemon_only_landing_kind(
    temp_db: HubDatabase, task: Task, roles: dict[str, str], receipt_tool: Any
) -> None:
    from gobby.mcp_proxy.tools.task_commits import create_commit_registry

    registry = create_commit_registry(task_manager=LocalTaskManager(temp_db))
    schema = registry.get_schema("record_close_receipt")
    assert schema is not None
    kinds = schema["inputSchema"]["properties"]["kind"]["enum"]
    assert kinds == sorted(CALLER_RECEIPT_KINDS)
    assert LANDING not in kinds
    with (
        session_context_for_test(roles["creator"]),
        patch("gobby.mcp_proxy.tools.task_commits.normalize_commit_sha") as verify_commit,
    ):
        result = await receipt_tool(task_id=task.id, kind=LANDING, commit_sha=_LANDED)
    assert "landing is written only by land_commit" in result["error"]
    verify_commit.assert_not_awaited()
    assert list_close_receipts(temp_db, task.id) == []


def test_landing_receipt_is_recorded_once_per_author_and_commit(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    facts = {"branch": "0.5.0", "landed_tip": _LANDED, "mode": "ff", "activation_class": "none"}
    first, created = _record(temp_db, task, roles["reviewer"], kind=LANDING, facts=facts)
    replay, replay_created = _record(temp_db, task, roles["reviewer"], kind=LANDING)
    another_author, author_created = _record(temp_db, task, roles["bystander"], kind=LANDING)
    another_commit, commit_created = _record(
        temp_db, task, roles["reviewer"], kind=LANDING, sha=_LANDED
    )
    assert created and author_created and commit_created
    assert replay_created is False
    assert replay == first
    assert first.facts == facts
    assert another_author.author_session_id == roles["bystander"]
    assert another_commit.commit_sha == _LANDED
    assert list_close_receipts(temp_db, task.id) == [first, another_author, another_commit]
    with pytest.raises(CloseReceiptError, match="claimant"):
        _record(temp_db, task, roles["claimant"], kind=LANDING)


def test_reviewer_prompt_explains_landing_receipts(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    _record(temp_db, task, roles["creator"], kind=LANDING_APPROVAL, facts={"reason": "restart"})
    landing_facts = {
        "branch": "0.5.0",
        "landed_tip": _TIP,
        "mode": "ff",
        "activation_class": "restart",
    }
    _record(temp_db, task, roles["reviewer"], kind=LANDING, facts=landing_facts)
    prompt = build_agentic_review_prompt(
        review_id="landing-review",
        task_id=task.id,
        commit_shas=[_TIP],
        changes_summary="Landed the approved commit.",
        review_fingerprint="fp",
        evidence_fingerprint="efp",
        criterion_count=1,
        close_receipts=close_receipt_facts(temp_db, task, [_TIP]),
    )
    rendered = prompt.split("close_receipts=", 1)[1].split(". close_receipts are", 1)[0]
    approval, landing = json.loads(rendered)
    assert approval["kind"] == LANDING_APPROVAL
    assert approval["author_role"] == "task_creator"
    assert approval["facts"] == {"reason": "restart"}
    assert landing["kind"] == LANDING
    assert landing["facts"] == landing_facts
    assert approval["matches_linked_commit"] is True
    assert landing["matches_linked_commit"] is True
    assert "creator's or delegator's approval to land that SHA despite the named reasons" in prompt
    assert "daemon's record that land_commit landed that SHA" in prompt
    assert "branch, landed tip, mode and activation class" in prompt
    assert "Neither is a verdict" in prompt


def test_landing_approval_new_reasons_append_receipt(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    first, first_created = _record(
        temp_db, task, roles["creator"], kind=LANDING_APPROVAL, facts={"reason": "restart"}
    )
    second, second_created = _record(
        temp_db, task, roles["creator"], kind=LANDING_APPROVAL, facts={"reason": "freeze,overlap"}
    )
    first_replay, first_replayed_created = _record(
        temp_db,
        task,
        roles["creator"],
        kind=LANDING_APPROVAL,
        facts={"reason": " restart, restart ", "batch": "ignored-on-replay"},
    )
    second_replay, second_replayed_created = _record(
        temp_db,
        task,
        roles["creator"],
        kind=LANDING_APPROVAL,
        facts={"reason": " overlap, freeze "},
    )
    assert first_created and second_created
    assert first_replayed_created is False
    assert second_replayed_created is False
    assert first_replay == first
    assert second_replay == second
    assert first.id != second.id
    assert first.facts == {"reason": "restart"}
    assert second.facts == {"reason": "freeze,overlap"}
    assert list_close_receipts(temp_db, task.id) == [first, second]


def test_activation_comes_only_from_task_creator_or_delegator(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    with pytest.raises(CloseReceiptError, match="creator or delegator"):
        _record(temp_db, task, roles["bystander"], kind=ACTIVATION, sha=_LANDED)

    receipt, created = _record(
        temp_db, task, roles["creator"], kind=ACTIVATION, sha=_LANDED, facts=_PROMOTION_FACTS
    )

    assert created is True
    assert receipt.facts == _PROMOTION_FACTS
    assert [stored.kind for stored in list_close_receipts(temp_db, task.id)] == [ACTIVATION]


@pytest.mark.parametrize(
    ("kind", "sha", "facts", "message"),
    [
        ("self_approval", _TIP, None, "kind must be one of"),
        (INDEPENDENT_REVIEW_APPROVAL, "871d706", None, "full 40-character"),
        (INDEPENDENT_REVIEW_APPROVAL, "g" * 40, None, "full 40-character"),
        (INDEPENDENT_REVIEW_APPROVAL, _TIP, ["pid"], "facts must be an object"),
        (INDEPENDENT_REVIEW_APPROVAL, _TIP, {f"k{i}": i for i in range(17)}, "at most 16"),
        (INDEPENDENT_REVIEW_APPROVAL, _TIP, {"k" * 65: 1}, "fact key"),
        (INDEPENDENT_REVIEW_APPROVAL, _TIP, {"pid": 1.5}, "string, integer, or boolean"),
        (INDEPENDENT_REVIEW_APPROVAL, _TIP, {"log": {"nested": 1}}, "string, integer"),
        (INDEPENDENT_REVIEW_APPROVAL, _TIP, {"log": "x" * 301}, "exceeds 300"),
    ],
)
def test_malformed_receipts_are_refused(
    temp_db: HubDatabase,
    task: Task,
    roles: dict[str, str],
    kind: str,
    sha: str,
    facts: object,
    message: str,
) -> None:
    with pytest.raises(CloseReceiptError, match=message):
        _record(temp_db, task, roles["reviewer"], kind=kind, sha=sha, facts=facts)
    assert list_close_receipts(temp_db, task.id) == []


def test_launch_facts_render_author_role_and_linked_commit_match(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    _record(temp_db, task, roles["reviewer"])
    _record(temp_db, task, roles["creator"], kind=ACTIVATION, sha=_LANDED, facts=_PROMOTION_FACTS)

    facts = close_receipt_facts(temp_db, task, [_LANDED[:10]])

    assert [
        {key: value for key, value in fact.items() if key != "recorded_at"} for fact in facts
    ] == [
        {
            "kind": INDEPENDENT_REVIEW_APPROVAL,
            "commit_sha": _TIP,
            "matches_linked_commit": False,
            "author_session": _seq_ref(temp_db, roles["reviewer"]),
            "author_role": "independent_session",
            "facts": {},
            "verdict": "LAND",
        },
        {
            "kind": ACTIVATION,
            "commit_sha": _LANDED,
            "matches_linked_commit": True,
            "author_session": _seq_ref(temp_db, roles["creator"]),
            "author_role": "task_creator",
            "facts": _PROMOTION_FACTS,
        },
    ]
    assert all(isinstance(fact["recorded_at"], str) and fact["recorded_at"] for fact in facts)


def test_task_without_receipts_renders_no_launch_facts(temp_db: HubDatabase, task: Task) -> None:
    assert close_receipt_facts(temp_db, task, [_LANDED]) == []


def test_promotion_activation_receipt_reaches_the_reviewer_prompt(
    temp_db: HubDatabase, task: Task, roles: dict[str, str]
) -> None:
    """#23096 regression: supported-promotion evidence must reach the close reviewer."""
    _record(temp_db, task, roles["creator"], kind=ACTIVATION, sha=_LANDED, facts=_PROMOTION_FACTS)

    prompt = build_agentic_review_prompt(
        review_id="review-1",
        task_id=task.id,
        commit_shas=[_LANDED],
        changes_summary="Promoted the binary set.",
        review_fingerprint="fp",
        evidence_fingerprint="efp",
        criterion_count=1,
        close_receipts=close_receipt_facts(temp_db, task, [_LANDED]),
    )

    rendered = prompt.split("close_receipts=", 1)[1].split(". close_receipts are", 1)[0]
    [receipt] = json.loads(rendered)
    assert receipt["kind"] == ACTIVATION
    assert receipt["commit_sha"] == _LANDED
    assert receipt["matches_linked_commit"] is True
    assert receipt["author_role"] == "task_creator"
    assert receipt["facts"] == _PROMOTION_FACTS
    assert "never an automatic verdict" in prompt


@pytest.fixture
def receipt_tool(temp_db: HubDatabase) -> Iterator[Any]:
    """The real record_close_receipt MCP tool with Git resolution stubbed."""
    from gobby.mcp_proxy.tools.task_commits import create_commit_registry

    registry = create_commit_registry(task_manager=LocalTaskManager(temp_db))
    with (
        patch(
            "gobby.mcp_proxy.tools.task_commits.resolve_task_repo_path",
            return_value="/repo",
        ),
        patch(
            "gobby.mcp_proxy.tools.task_commits.normalize_commit_sha",
            side_effect=lambda sha, **_kwargs: None if sha == _TIP else sha,
        ),
    ):
        yield registry.get_tool("record_close_receipt")


async def test_tool_takes_author_from_calling_session(
    temp_db: HubDatabase, task: Task, roles: dict[str, str], receipt_tool: Any
) -> None:
    with session_context_for_test(roles["creator"]):
        result = await receipt_tool(
            task_id=task.id, kind=ACTIVATION, commit_sha=_LANDED, facts=_PROMOTION_FACTS
        )
        replay = await receipt_tool(task_id=task.id, kind=ACTIVATION, commit_sha=_LANDED)

    assert result["created"] is True
    assert result["facts"] == _PROMOTION_FACTS
    assert replay == {**result, "created": False}
    [stored] = list_close_receipts(temp_db, task.id)
    assert stored.author_session_id == roles["creator"]


async def test_tool_refuses_claimant_unknown_commit_and_missing_session(
    temp_db: HubDatabase, task: Task, roles: dict[str, str], receipt_tool: Any
) -> None:
    with session_context_for_test(roles["claimant"]):
        claimant = await receipt_tool(task_id=task.id, kind=ACTIVATION, commit_sha=_LANDED)
    with session_context_for_test(roles["reviewer"]):
        unknown = await receipt_tool(
            task_id=task.id, kind=INDEPENDENT_REVIEW_APPROVAL, commit_sha=_TIP
        )
    anonymous = await receipt_tool(task_id=task.id, kind=ACTIVATION, commit_sha=_LANDED)

    assert "claimant" in claimant["error"]
    assert unknown == {"error": f"Invalid or unresolved commit SHA: {_TIP}"}
    assert "No session context" in anonymous["error"]
    assert list_close_receipts(temp_db, task.id) == []


def _mutate_during_commit_check(db: HubDatabase, sql: str, params: tuple[str, ...]) -> Any:
    """Patch commit verification to change the task row while the tool awaits it."""

    def verify(sha: str, **_kwargs: object) -> str:
        db.execute(sql, params)
        return sha

    return patch("gobby.mcp_proxy.tools.task_commits.normalize_commit_sha", side_effect=verify)


async def test_tool_refuses_author_who_claims_the_task_during_commit_check(
    temp_db: HubDatabase, task: Task, roles: dict[str, str], receipt_tool: Any
) -> None:
    claim = "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s"
    with (
        session_context_for_test(roles["reviewer"]),
        _mutate_during_commit_check(temp_db, claim, (roles["reviewer"], task.id)),
    ):
        result = await receipt_tool(
            task_id=task.id, kind=INDEPENDENT_REVIEW_APPROVAL, commit_sha=_LANDED
        )

    assert "claimant" in result["error"]
    assert list_close_receipts(temp_db, task.id) == []


async def test_tool_refuses_activation_from_delegator_replaced_during_commit_check(
    temp_db: HubDatabase, task: Task, roles: dict[str, str], receipt_tool: Any
) -> None:
    delegate = "UPDATE tasks SET delegated_by_session_id = %s WHERE id = %s"
    temp_db.execute(delegate, (roles["bystander"], task.id))
    with (
        session_context_for_test(roles["bystander"]),
        _mutate_during_commit_check(temp_db, delegate, (roles["creator"], task.id)),
    ):
        result = await receipt_tool(task_id=task.id, kind=ACTIVATION, commit_sha=_LANDED)

    assert "creator or delegator" in result["error"]
    assert list_close_receipts(temp_db, task.id) == []
