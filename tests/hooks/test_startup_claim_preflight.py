"""AGY startup-claim preflight: resolve-or-adopt-or-register plus the bounded wrapper."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from gobby.hooks.startup_claim_preflight import (
    StartupClaimLease,
    StartupClaimPreflightTimeout,
    _hint_mismatch,
    preflight_agy_startup_claim,
    preflight_agy_startup_claim_bounded,
    preflight_timeout_seconds,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
CONVERSATION = "agy-conversation-1"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _workspace(tmp_path: Path, name: str, project_id: str) -> str:
    workspace = tmp_path / name
    (workspace / ".gobby").mkdir(parents=True)
    (workspace / ".gobby" / "project.json").write_text(
        json.dumps({"id": project_id, "name": name}),
        encoding="utf-8",
    )
    return str(workspace)


def _payload(
    *,
    workspace: str,
    conversation_id: str = CONVERSATION,
    hint: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": "agy",
        "hook_type": "PreInvocation",
        "input_data": {
            "hookEventName": "PreInvocation",
            "conversationId": conversation_id,
            "cwd": workspace,
            "workspacePaths": [workspace],
        },
    }
    if hint is not None:
        payload["_platform_session_id"] = hint
    payload.update(extra)
    return payload


def _hook_manager(db: HubDatabase) -> SimpleNamespace:
    return SimpleNamespace(session_manager=SessionManager(db))


def _project(db: HubDatabase, name: str) -> str:
    return LocalProjectManager(db).create(name=f"{name}-{uuid4().hex[:8]}", repo_path=f"/{name}").id


def _rows_for_conversation(db: HubDatabase, conversation_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.fetchall(
            "SELECT id, external_id, session_type, project_id, workspace_path, "
            "startup_claim_state, startup_claim_owner, startup_claim_generation, updated_at "
            "FROM sessions WHERE external_id = %s ORDER BY created_at",
            (conversation_id,),
        )
    ]


class TestResolveOrAdoptOrRegister:
    def test_first_event_registers_the_canonical_row_once(
        self, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        project_id = _project(temp_db, "first-event")
        workspace = _workspace(tmp_path, "ws", project_id)
        payload = _payload(workspace=workspace)

        lease = preflight_agy_startup_claim(payload, _hook_manager(temp_db))

        assert lease is not None
        rows = _rows_for_conversation(temp_db, CONVERSATION)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == lease.session_id
        assert row["session_type"] == "terminal"
        assert row["project_id"] == project_id
        assert row["workspace_path"] == workspace
        assert row["startup_claim_state"] == "claimed"
        assert row["startup_claim_owner"] == lease.owner_token
        assert int(row["startup_claim_generation"]) == lease.generation == 1
        assert payload["_gobby_startup_claim"] == {
            "session_id": lease.session_id,
            "generation": 1,
            "owner_token": lease.owner_token,
        }
        session = SessionManager(temp_db).get(lease.session_id)
        assert session is not None
        assert session.machine_id == LOCAL_MACHINE_ID
        assert session.source == "agy"

    def test_repeated_event_is_idempotent(self, temp_db: HubDatabase, tmp_path: Path) -> None:
        project_id = _project(temp_db, "repeat")
        workspace = _workspace(tmp_path, "ws", project_id)
        manager = _hook_manager(temp_db)

        first = preflight_agy_startup_claim(_payload(workspace=workspace), manager)
        assert first is not None
        while_claimed = preflight_agy_startup_claim(_payload(workspace=workspace), manager)
        assert while_claimed is None

        assert SessionVariableManager(temp_db).commit_startup_context(
            first.session_id, first.generation, first.owner_token
        )
        after_commit = preflight_agy_startup_claim(_payload(workspace=workspace), manager)
        assert after_commit is None
        rows = _rows_for_conversation(temp_db, CONVERSATION)
        assert [row["id"] for row in rows] == [first.session_id]
        assert rows[0]["startup_claim_state"] == "committed"

    def test_concurrent_events_commit_once(self, temp_db: HubDatabase, tmp_path: Path) -> None:
        project_id = _project(temp_db, "concurrent")
        workspace = _workspace(tmp_path, "ws", project_id)
        manager = _hook_manager(temp_db)
        barrier = threading.Barrier(6)

        def run(_: int) -> StartupClaimLease | None:
            barrier.wait()
            return preflight_agy_startup_claim(_payload(workspace=workspace), manager)

        with ThreadPoolExecutor(max_workers=6) as executor:
            leases = [lease for lease in executor.map(run, range(6)) if lease is not None]

        assert len(leases) == 1
        rows = _rows_for_conversation(temp_db, CONVERSATION)
        assert len(rows) == 1
        variables = SessionVariableManager(temp_db)
        lease = leases[0]
        assert variables.commit_startup_context(
            lease.session_id, lease.generation, lease.owner_token
        )
        assert not variables.commit_startup_context(
            lease.session_id, lease.generation, lease.owner_token
        )

    def test_pre_created_child_adopts_the_hint_and_binds_conversation(
        self, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        project_id = _project(temp_db, "child")
        workspace = _workspace(tmp_path, "ws", project_id)
        sessions = SessionManager(temp_db)
        child = sessions.register(
            external_id="agent-precreated01",
            machine_id=LOCAL_MACHINE_ID,
            source="agy",
            project_id=project_id,
            workspace_path=workspace,
        )

        lease = preflight_agy_startup_claim(
            _payload(workspace=workspace, hint=child.id), _hook_manager(temp_db)
        )

        assert lease is not None
        assert lease.session_id == child.id
        adopted = sessions.get(child.id)
        assert adopted is not None
        assert adopted.external_id == CONVERSATION
        assert adopted.session_type == "terminal"
        assert adopted.startup_claim_state == "claimed"
        assert adopted.startup_claim_owner == lease.owner_token
        assert len(_rows_for_conversation(temp_db, CONVERSATION)) == 1

    def test_terminal_and_web_chat_collision_each_commit_once(
        self, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        project_id = _project(temp_db, "collision")
        workspace = _workspace(tmp_path, "ws", project_id)
        sessions = SessionManager(temp_db)
        web = sessions.register(
            external_id=CONVERSATION,
            machine_id=LOCAL_MACHINE_ID,
            source="agy",
            project_id=project_id,
            session_type="web_chat",
            workspace_path=workspace,
        )
        manager = _hook_manager(temp_db)

        web_lease = preflight_agy_startup_claim(
            _payload(workspace=workspace, hint=web.id, session_type="web_chat"), manager
        )
        terminal_lease = preflight_agy_startup_claim(_payload(workspace=workspace), manager)

        assert web_lease is not None and web_lease.session_id == web.id
        assert terminal_lease is not None and terminal_lease.session_id != web.id
        rows = {row["session_type"]: row for row in _rows_for_conversation(temp_db, CONVERSATION)}
        assert set(rows) == {"web_chat", "terminal"}
        assert rows["web_chat"]["id"] == web.id
        variables = SessionVariableManager(temp_db)
        for lease in (web_lease, terminal_lease):
            assert variables.commit_startup_context(
                lease.session_id, lease.generation, lease.owner_token
            )
            assert not variables.commit_startup_context(
                lease.session_id, lease.generation, lease.owner_token
            )

    def test_mismatched_hint_falls_through_without_mutating_the_hinted_row(
        self, temp_db: HubDatabase, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        project_a = _project(temp_db, "hint-a")
        project_b = _project(temp_db, "hint-b")
        workspace_a = _workspace(tmp_path, "ws-a", project_a)
        workspace_b = _workspace(tmp_path, "ws-b", project_b)
        sessions = SessionManager(temp_db)
        hinted = sessions.register(
            external_id=CONVERSATION,
            machine_id=LOCAL_MACHINE_ID,
            source="agy",
            project_id=project_a,
            workspace_path=workspace_a,
        )
        before = sessions.get(hinted.id)
        assert before is not None
        payload = _payload(workspace=workspace_b, hint=hinted.id)

        with caplog.at_level(logging.WARNING, logger="gobby.hooks.startup_claim_preflight"):
            lease = preflight_agy_startup_claim(payload, _hook_manager(temp_db))

        # The rejected row owns this conversation identity: ordinary resolution
        # must neither adopt it nor recover/move it, so no claim is made.
        assert lease is None
        after = sessions.get(hinted.id)
        assert after is not None
        assert after.updated_at == before.updated_at
        assert after.startup_claim_state == "idle"
        assert after.workspace_path == workspace_a
        assert after.project_id == project_a
        assert [row["id"] for row in _rows_for_conversation(temp_db, CONVERSATION)] == [hinted.id]
        diagnostic = payload["_gobby_session_hint_error"]
        assert hinted.id in diagnostic
        assert "workspace_path=" in diagnostic
        assert any(
            record.levelno == logging.WARNING and diagnostic in record.getMessage()
            for record in caplog.records
        )
        assert "_gobby_startup_claim" not in payload

    def test_mismatched_hint_for_another_conversation_registers_fresh_row(
        self, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        project_a = _project(temp_db, "other-a")
        project_b = _project(temp_db, "other-b")
        workspace_a = _workspace(tmp_path, "ws-a", project_a)
        workspace_b = _workspace(tmp_path, "ws-b", project_b)
        sessions = SessionManager(temp_db)
        hinted = sessions.register(
            external_id="agy-conversation-other",
            machine_id=LOCAL_MACHINE_ID,
            source="agy",
            project_id=project_a,
            workspace_path=workspace_a,
        )
        before = sessions.get(hinted.id)
        assert before is not None
        payload = _payload(workspace=workspace_b, hint=hinted.id)

        lease = preflight_agy_startup_claim(payload, _hook_manager(temp_db))

        assert lease is not None
        assert lease.session_id != hinted.id
        after = sessions.get(hinted.id)
        assert after is not None
        assert after.updated_at == before.updated_at
        assert after.external_id == "agy-conversation-other"
        assert after.startup_claim_state == "idle"
        registered = sessions.get(lease.session_id)
        assert registered is not None
        assert registered.project_id == project_b
        assert registered.external_id == CONVERSATION
        assert registered.startup_claim_state == "claimed"

    def test_preflight_adopts_owner_of_a_released_startup_receipt(
        self, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        """A lost delivery re-presents: the next preflight adopts the released owner token."""
        from gobby.storage.hook_receipts import prepare_receipt, release_receipt

        project_id = _project(temp_db, "redeliver")
        workspace = _workspace(tmp_path, "ws", project_id)
        manager = _hook_manager(temp_db)
        lost = preflight_agy_startup_claim(_payload(workspace=workspace), manager)
        assert lost is not None
        receipt = prepare_receipt(
            temp_db,
            session_id=lost.session_id,
            envelope_id="env-lost",
            staged_payload={
                "startup_context": {
                    "generation": lost.generation,
                    "owner_token": lost.owner_token,
                    "session_id": lost.session_id,
                }
            },
        )
        assert release_receipt(temp_db, receipt_id=receipt.receipt_id) is not None

        again = preflight_agy_startup_claim(_payload(workspace=workspace), manager)

        assert again is not None
        assert again.session_id == lost.session_id
        assert again.owner_token == lost.owner_token
        assert again.generation == lost.generation


def _hinted_row(**overrides: Any) -> SimpleNamespace:
    row = SimpleNamespace(
        id="sess-hint",
        project_id="proj-agy",
        source="agy",
        machine_id=LOCAL_MACHINE_ID,
        session_type="terminal",
        workspace_path="/ws",
        workspace_generation=1,
        status="active",
        external_id=CONVERSATION,
        transcript_path=None,
        message_count=0,
        turn_count=0,
        tombstoned=False,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


_HINT_MISMATCH_CASES: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {
    "wrong-project": ({}, {"project_id": "proj-other"}, "project_id="),
    "wrong-source": ({"source": "claude"}, {}, "source="),
    "wrong-machine": ({}, {"machine_id": "machine-elsewhere"}, "machine_id="),
    "wrong-session-type": ({}, {"session_type": "web_chat"}, "session_type="),
    "wrong-worktree": ({"workspace_path": "/elsewhere"}, {}, "workspace_path="),
    "null-worktree": ({"workspace_path": None}, {}, "workspace_path=None"),
    "tombstoned-workspace": ({"tombstoned": True}, {}, "tombstoned"),
    "pending-transcript": (
        {"external_id": "agy-conversation-other", "transcript_path": "/t.jsonl"},
        {},
        "pending transcript",
    ),
    "concurrent-switch": ({}, {"workspace_generation": 2}, "concurrent workspace switch"),
    "dead-session": ({"status": "expired"}, {}, "no longer live"),
}


class TestHintValidation:
    @pytest.mark.parametrize("case", sorted(_HINT_MISMATCH_CASES))
    def test_mismatching_hint_rejects_without_mutation(
        self, case: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        row_overrides, payload_extra, expected = _HINT_MISMATCH_CASES[case]
        row = _hinted_row(**row_overrides)
        sessions = MagicMock()
        sessions.get.return_value = row
        sessions.db = object()
        sessions.find_by_external_id.return_value = None
        sessions.register.return_value = None
        payload = _payload(workspace="/ws", hint="sess-hint", project_id="proj-agy")
        payload.update(payload_extra)
        claims: list[str] = []

        def claim(_self: object, session_id: str, owner_token: str | None = None) -> Any:
            claims.append(session_id)
            return SimpleNamespace(mode="full", state="claimed", owner_token=owner_token)

        with (
            caplog.at_level(logging.WARNING, logger="gobby.hooks.startup_claim_preflight"),
            patch(
                "gobby.hooks.startup_claim_preflight.SessionVariableManager.claim_startup_context",
                claim,
            ),
        ):
            lease = preflight_agy_startup_claim(payload, SimpleNamespace(session_manager=sessions))

        assert lease is None
        assert claims == []
        sessions.update.assert_not_called()
        diagnostic = payload["_gobby_session_hint_error"]
        assert "sess-hint" in diagnostic
        assert expected in diagnostic
        assert any(
            record.levelno == logging.WARNING and diagnostic in record.getMessage()
            for record in caplog.records
        )
        assert "_gobby_startup_claim" not in payload

    def test_null_row_columns_are_mismatches_not_wildcards(self) -> None:
        row = _hinted_row(source=None, project_id=None, machine_id=None, session_type=None)
        payload = _payload(
            workspace="/ws",
            hint="sess-hint",
            project_id="proj-agy",
            machine_id=LOCAL_MACHINE_ID,
            session_type="terminal",
        )
        diagnostic = _hint_mismatch(row, payload, CONVERSATION)
        assert diagnostic is not None
        for field in ("source=None", "project_id=None", "machine_id=None", "session_type=None"):
            assert field in diagnostic

    def test_fully_matching_hint_has_no_diagnostic(self) -> None:
        payload = _payload(
            workspace="/ws",
            hint="sess-hint",
            project_id="proj-agy",
            machine_id=LOCAL_MACHINE_ID,
            session_type="terminal",
            workspace_generation=1,
        )
        assert _hint_mismatch(_hinted_row(), payload, CONVERSATION) is None

    def test_unbound_placeholder_row_is_adoptable(self) -> None:
        row = _hinted_row(external_id="agent-abc123")
        assert (
            _hint_mismatch(row, _payload(workspace="/ws", hint="sess-hint"), CONVERSATION) is None
        )


class TestBoundedPreflight:
    def test_timeout_bound_is_capped_by_the_adapter_budget(self) -> None:
        assert preflight_timeout_seconds(0.15) == 0.15
        assert preflight_timeout_seconds(105.0) == 5.0

    @pytest.mark.asyncio
    async def test_non_agy_payload_short_circuits(self) -> None:
        with patch("gobby.hooks.startup_claim_preflight.preflight_agy_startup_claim") as sync:
            result = await preflight_agy_startup_claim_bounded(
                {"source": "claude", "hook_type": "session-start"},
                SimpleNamespace(),
                timeout_seconds=1.0,
            )
        assert result is None
        sync.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_preflight_returns_its_lease(self) -> None:
        lease = StartupClaimLease("sess-1", 2, "owner-2")
        with patch(
            "gobby.hooks.startup_claim_preflight.preflight_agy_startup_claim",
            return_value=lease,
        ):
            result = await preflight_agy_startup_claim_bounded(
                _payload(workspace="/ws"),
                SimpleNamespace(),
                timeout_seconds=1.0,
            )
        assert result == lease

    @pytest.mark.asyncio
    async def test_timeout_raises_and_late_lease_is_invalidated(self) -> None:
        gate = threading.Event()
        lease = StartupClaimLease("sess-late", 4, "owner-late")
        hook_manager = SimpleNamespace(session_manager=None)

        def slow_preflight(_payload: dict[str, Any], _hook_manager: Any) -> StartupClaimLease:
            assert gate.wait(timeout=5)
            return lease

        with (
            patch(
                "gobby.hooks.startup_claim_preflight.preflight_agy_startup_claim",
                slow_preflight,
            ),
            patch("gobby.hooks.startup_claim_preflight.invalidate_agy_startup_claim") as invalidate,
        ):
            with pytest.raises(StartupClaimPreflightTimeout):
                await preflight_agy_startup_claim_bounded(
                    _payload(workspace="/ws"),
                    hook_manager,
                    timeout_seconds=0.02,
                )
            invalidate.assert_not_called()
            gate.set()
            for _ in range(200):
                if invalidate.call_args_list:
                    break
                await asyncio.sleep(0.01)
            invalidate.assert_called_once_with(hook_manager, lease)

    @pytest.mark.asyncio
    async def test_cancelled_request_invalidates_late_lease(self) -> None:
        gate = threading.Event()
        lease = StartupClaimLease("sess-cancel", 1, "owner-cancel")
        hook_manager = SimpleNamespace(session_manager=None)

        def slow_preflight(_payload: dict[str, Any], _hook_manager: Any) -> StartupClaimLease:
            assert gate.wait(timeout=5)
            return lease

        with (
            patch(
                "gobby.hooks.startup_claim_preflight.preflight_agy_startup_claim",
                slow_preflight,
            ),
            patch("gobby.hooks.startup_claim_preflight.invalidate_agy_startup_claim") as invalidate,
        ):
            task = asyncio.ensure_future(
                preflight_agy_startup_claim_bounded(
                    _payload(workspace="/ws"),
                    hook_manager,
                    timeout_seconds=5.0,
                )
            )
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            gate.set()
            for _ in range(200):
                if invalidate.call_args_list:
                    break
                await asyncio.sleep(0.01)
            invalidate.assert_called_once_with(hook_manager, lease)
