"""Hub-wide attention roster: every machine's entries, each with its machine and project."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.agents.prompt_detector import PromptDetector
from gobby.servers.http import HTTPServer
from gobby.servers.routes.attention import AttentionAnswer, AttentionPane, create_attention_router
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.attention import AttentionState, AttentionStateManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory, insert_isolated_machine

pytestmark = pytest.mark.unit

DETECTION_REGISTRY = BundledDetectionRegistry()
REMOTE_MACHINE_ID = "eeeeeeee-eeee-4eee-8eee-000000024277"
QUESTION_PROMPT = "Would you like to continue?\n1. Yes\n2. No\n"


async def _run_db(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return function(*args, **kwargs)


def _client(
    temp_db: HubDatabase,
    manager: AttentionStateManager,
    *,
    detector_root: PromptDetector,
    capture: Callable[[], Awaitable[str | None]] | None = None,
    injected: list[AttentionAnswer] | None = None,
) -> TestClient:
    async def resolve_pane(_state: AttentionState) -> AttentionPane | None:
        return None if capture is None else AttentionPane(target="%42", capture=capture)

    async def inject(_pane: AttentionPane, answer: AttentionAnswer) -> None:
        if injected is not None:
            injected.append(answer)

    server = SimpleNamespace(
        services=SimpleNamespace(
            attention_manager=manager,
            agent_lifecycle_monitor=SimpleNamespace(prompt_detector=detector_root),
            database=temp_db,
            run_db=_run_db,
        )
    )
    app = FastAPI()
    app.include_router(
        create_attention_router(
            cast(HTTPServer, server), pane_resolver=resolve_pane, injector=inject
        )
    )
    return TestClient(app)


def _roster(temp_db: HubDatabase) -> dict[str, dict[str, Any]]:
    detector = PromptDetector(DETECTION_REGISTRY, "claude")
    with _client(temp_db, AttentionStateManager(temp_db), detector_root=detector) as client:
        response = client.get("/api/attention/roster")
    assert response.status_code == 200
    return {entry["entry_id"]: entry for entry in response.json()["entries"]}


def _session(
    session_manager: SessionManager, project_id: str, name: str, *, pane: str | None = None
) -> Session:
    return session_manager.register(
        external_id=f"roster-machines-{name}",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
        terminal_context={"tmux_pane": pane} if pane is not None else None,
    )


def _run(temp_db: HubDatabase, parent: Session, *, child: Session | None = None) -> AgentRun:
    return LocalAgentRunManager(temp_db).create(
        parent_session_id=parent.id,
        child_session_id=child.id if child is not None else None,
        provider="claude",
        prompt="test",
    )


def _move_session_to_remote(temp_db: HubDatabase, session: Session) -> None:
    insert_isolated_machine(temp_db, REMOTE_MACHINE_ID)
    temp_db.execute(
        "UPDATE sessions SET machine_id = %s WHERE id = %s", (REMOTE_MACHINE_ID, session.id)
    )


def _move_run_to_remote(temp_db: HubDatabase, run: AgentRun) -> None:
    insert_isolated_machine(temp_db, REMOTE_MACHINE_ID)
    temp_db.execute(
        "UPDATE agent_runs SET machine_id = %s WHERE id = %s", (REMOTE_MACHINE_ID, run.id)
    )


def test_roster_is_hub_wide(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    local_session = _session(session_manager, sample_project["id"], "local", pane="%1")
    remote_session = _session(session_manager, sample_project["id"], "remote", pane="%2")
    _move_session_to_remote(temp_db, remote_session)
    local_run = _run(temp_db, local_session)
    remote_run = _run(temp_db, local_session)
    _move_run_to_remote(temp_db, remote_run)

    entries = _roster(temp_db)

    assert {
        f"run:{local_run.id}",
        f"run:{remote_run.id}",
        f"session:{local_session.id}",
        f"session:{remote_session.id}",
    } <= set(entries)


def test_entries_carry_machine_and_project(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    isolated_checkout_factory: IsolatedCheckoutFactory,
) -> None:
    local_machine_id = require_machine_id()
    other_project_id = isolated_checkout_factory(temp_db, "roster-machines-other").project.id
    parent = _session(session_manager, sample_project["id"], "parent", pane="%1")
    child = _session(session_manager, other_project_id, "child")
    remote_session = _session(session_manager, other_project_id, "remote", pane="%2")
    _move_session_to_remote(temp_db, remote_session)
    childless_run = _run(temp_db, parent)
    remote_child_run = _run(temp_db, parent, child=child)
    _move_run_to_remote(temp_db, remote_child_run)

    entries = _roster(temp_db)

    observed = {
        entry_id: (entries[entry_id]["machine_id"], entries[entry_id]["project_id"])
        for entry_id in (
            f"session:{parent.id}",
            f"session:{remote_session.id}",
            f"run:{childless_run.id}",
            f"run:{remote_child_run.id}",
        )
    }
    assert observed == {
        f"session:{parent.id}": (local_machine_id, sample_project["id"]),
        f"session:{remote_session.id}": (REMOTE_MACHINE_ID, other_project_id),
        f"run:{childless_run.id}": (local_machine_id, sample_project["id"]),
        f"run:{remote_child_run.id}": (REMOTE_MACHINE_ID, other_project_id),
    }


def test_local_respond_resolves_detector(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    parent = _session(session_manager, sample_project["id"], "respond-parent")
    local_run = _run(temp_db, parent)
    remote_run = _run(temp_db, parent)
    _move_run_to_remote(temp_db, remote_run)
    manager = AttentionStateManager(temp_db)
    prompt = PromptDetector(DETECTION_REGISTRY, "claude").detect_prompt(QUESTION_PROMPT)
    assert prompt is not None
    opened = manager.transition(
        f"run:{local_run.id}",
        state="blocked",
        run_id=local_run.id,
        session_id=parent.id,
        reason=prompt.kind,
        kind="actionable",
        fingerprint=prompt.fingerprint,
        payload=prompt.to_payload(),
    ).current
    assert opened is not None
    assert opened.fingerprint is not None
    injected: list[AttentionAnswer] = []

    async def capture() -> str:
        return QUESTION_PROMPT

    # A provider-neutral root resolves the run's provider from the roster rows.
    with _client(
        temp_db,
        manager,
        detector_root=PromptDetector(DETECTION_REGISTRY),
        capture=capture,
        injected=injected,
    ) as client:
        response = client.post(
            f"/api/attention/{opened.entry_id}/respond",
            json={
                "attention_id": opened.attention_id,
                "fingerprint": opened.fingerprint,
                "answer": {"option": 1},
            },
        )

    assert response.status_code == 200, response.text
    assert [answer.option for answer in injected] == [1]
