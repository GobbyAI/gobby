"""Web-chat clear orchestration, row swap, and queue semantics."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.hooks.event_handlers._session_start.claims import preserve_task_claim_state
from gobby.hooks.hook_types import SessionEndReason
from gobby.llm.claude_models import DoneEvent
from gobby.servers.websocket.chat._session import ChatSessionMixin
from gobby.servers.websocket.chat.session_registry import (
    ClearLifecycleHooks,
    WebChatSessionRegistry,
)
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.sessions.clear_continuation import (
    CLEAR_ATTEMPT_VARIABLE,
    commit_web_chat_clear_successor,
    stage_clear_attempt,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "clear-attempt-web-1"
HANDOFF = "Continue epic #20539: web-chat clear successor must resume this work."


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


async def _done_stream() -> Any:
    yield DoneEvent(tool_calls_count=0)


def _live_session(*, db_session_id: str = "pred-db") -> MagicMock:
    session = MagicMock()
    session.db_session_id = db_session_id
    session.seq_num = 11
    session.message_index = 4
    session.conversation_id = "conv-1"
    session.provider = "claude"
    session.model = "claude-opus"
    session.chat_mode = "code"
    session.project_id = "proj-1"
    session.clear_context = AsyncMock(return_value=True)
    session.send_message.side_effect = lambda _message: _done_stream()
    session._on_mode_persist = None
    session._on_approved_tools_persist = None
    return session


class _FakeHooks:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: dict[str, Any] = {
            "ok": True,
            "successor_id": "succ-db",
            "predecessor_id": "pred-db",
            "seq_num": 12,
        }

    async def commit_clear_successor(
        self,
        *,
        conversation_id: str,
        session: Any,
        predecessor_id: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "conversation_id": conversation_id,
                "session": session,
                "predecessor_id": predecessor_id,
                "attempt_id": attempt_id,
            }
        )
        return dict(self.result)


def test_clear_session_signature_and_hooks_protocol() -> None:
    signature = inspect.signature(WebChatSessionRegistry.clear_session)
    assert list(signature.parameters) == ["self", "session_id", "attempt_id", "continuation_prompt"]
    assert signature.parameters["attempt_id"].kind is inspect.Parameter.KEYWORD_ONLY
    commit = inspect.signature(ClearLifecycleHooks.commit_clear_successor)
    assert "predecessor_id" in commit.parameters
    assert "attempt_id" in commit.parameters


def test_websocket_server_binds_clear_lifecycle_hooks() -> None:
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
        session_manager=MagicMock(db=MagicMock()),
    )
    hooks = server.web_chat_session_registry._clear_hooks
    assert hooks is server
    assert callable(getattr(hooks, "commit_clear_successor", None))


class TestClearSessionRegistry:
    @pytest.mark.asyncio
    async def test_uses_bound_hooks_and_does_not_touch_session_manager(self) -> None:
        registry = WebChatSessionRegistry()
        hooks = _FakeHooks()
        registry.bind_clear_lifecycle(hooks, db=MagicMock())
        session = _live_session()
        session._session_manager_ref = MagicMock()
        registry.register("conv-1", session)

        result = await registry.clear_session(
            "pred-db",
            attempt_id=ATTEMPT_ID,
            continuation_prompt="continue",
        )

        assert result["cleared"] is True
        assert result["queued"] is False
        assert result["successor_id"] == "succ-db"
        assert hooks.calls == [
            {
                "conversation_id": "conv-1",
                "session": session,
                "predecessor_id": "pred-db",
                "attempt_id": ATTEMPT_ID,
            }
        ]
        session.clear_context.assert_awaited_once_with()
        session._session_manager_ref.assert_not_called()
        assert [call.args[0] for call in session.send_message.call_args_list] == ["continue"]

    @pytest.mark.asyncio
    async def test_failed_clear_context_leaves_predecessor_and_fails_attempt(self) -> None:
        registry = WebChatSessionRegistry()
        hooks = _FakeHooks()
        registry.bind_clear_lifecycle(hooks, db=MagicMock())
        session = _live_session()
        session.clear_context = AsyncMock(return_value=False)
        registry.register("conv-1", session)

        with patch(
            "gobby.servers.websocket.chat.session_registry.clear_failed_attempt",
            return_value=True,
        ) as fail_attempt:
            result = await registry.clear_session(
                "pred-db",
                attempt_id=ATTEMPT_ID,
                continuation_prompt="continue",
            )

        assert result["cleared"] is False
        assert result["queued"] is not True
        assert session.db_session_id == "pred-db"
        assert hooks.calls == []
        fail_attempt.assert_called_once()
        assert fail_attempt.call_args.kwargs["attempt_id"] == ATTEMPT_ID
        session.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_commit_failure_after_clear_keeps_predecessor_and_fails_attempt(self) -> None:
        registry = WebChatSessionRegistry()
        hooks = _FakeHooks()
        hooks.result = {"ok": False, "reason": "commit exploded"}
        registry.bind_clear_lifecycle(hooks, db=MagicMock())
        session = _live_session()
        registry.register("conv-1", session)

        with patch(
            "gobby.servers.websocket.chat.session_registry.clear_failed_attempt",
            return_value=True,
        ) as fail_attempt:
            result = await registry.clear_session(
                "pred-db",
                attempt_id=ATTEMPT_ID,
                continuation_prompt="continue",
            )

        assert result["cleared"] is False
        assert session.db_session_id == "pred-db"
        session.clear_context.assert_awaited_once_with()
        fail_attempt.assert_called_once()
        session.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_turn_queues_without_reporting_success_and_coalesces(self) -> None:
        registry = WebChatSessionRegistry()
        hooks = _FakeHooks()
        registry.bind_clear_lifecycle(hooks, db=MagicMock())
        session = _live_session()
        registry.register("conv-1", session)
        release = asyncio.Event()

        async def active_turn() -> None:
            await release.wait()

        active_task = asyncio.create_task(active_turn())
        registry.track_active_task("conv-1", active_task)

        first = await registry.clear_session(
            "pred-db",
            attempt_id=ATTEMPT_ID,
            continuation_prompt="continue",
        )
        second = await registry.clear_session(
            "pred-db",
            attempt_id="clear-attempt-web-2",
            continuation_prompt="other",
        )

        assert first == {"queued": True, "attempt_id": ATTEMPT_ID}
        assert second == {"queued": True, "attempt_id": ATTEMPT_ID}
        assert first.get("cleared") is not True
        session.clear_context.assert_not_awaited()
        assert hooks.calls == []

        release.set()
        await active_task
        await drain_asyncio_tasks()
        queued_task = registry._queued_clear_tasks.get("conv-1")
        assert queued_task is not None
        await queued_task

        session.clear_context.assert_awaited_once_with()
        assert hooks.calls[0]["attempt_id"] == ATTEMPT_ID

    @pytest.mark.asyncio
    async def test_unregister_fails_pending_attempt(self) -> None:
        registry = WebChatSessionRegistry()
        hooks = _FakeHooks()
        registry.bind_clear_lifecycle(hooks, db=MagicMock())
        session = _live_session()
        registry.register("conv-1", session)
        release = asyncio.Event()

        async def active_turn() -> None:
            await release.wait()

        active_task = asyncio.create_task(active_turn())
        registry.track_active_task("conv-1", active_task)
        await registry.clear_session(
            "pred-db",
            attempt_id=ATTEMPT_ID,
            continuation_prompt="continue",
        )

        with patch(
            "gobby.servers.websocket.chat.session_registry.clear_failed_attempt",
            return_value=True,
        ) as fail_attempt:
            registry.unregister("conv-1")

        fail_attempt.assert_called_once()
        assert fail_attempt.call_args.args[1] == "pred-db"
        assert fail_attempt.call_args.kwargs["attempt_id"] == ATTEMPT_ID
        release.set()
        active_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active_task


class _CommitMixin(ChatSessionMixin):
    def __init__(self) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_modes: dict[str, str] = {}
        self._pending_worktree_paths: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}
        self._pending_projects: dict[str, str] = {}
        self._pending_providers: dict[str, str] = {}
        self._session_create_locks: dict[str, asyncio.Lock] = {}
        self.session_manager: Any = MagicMock()
        self.session_manager.db = MagicMock()
        self.daemon_config: Any = None
        self.web_chat_runtime_manager: Any = MagicMock()
        self.config_runtime: Any = None
        self.event_handlers: Any = MagicMock()
        self._ended: list[tuple[str, SessionEndReason | None, str | None]] = []

    async def _fire_lifecycle(self, cid: str, event_type: str, data: object) -> None:
        return None

    async def _fire_session_end(
        self,
        conversation_id: str,
        *,
        reason: SessionEndReason | None = None,
    ) -> None:
        # Record the db id the lifecycle handler would resolve from the live wrapper.
        live = self._chat_sessions[conversation_id]
        self._ended.append((conversation_id, reason, live.db_session_id))


class TestCommitClearSuccessor:
    @pytest.mark.asyncio
    async def test_rebinds_live_wrapper_and_fires_session_end(self) -> None:
        mixin = _CommitMixin()
        session = _live_session()
        session._on_mode_persist = MagicMock()
        mixin._chat_sessions["conv-1"] = session
        successor = MagicMock()
        successor.id = "succ-db"
        successor.seq_num = 99
        persist_targets: list[str] = []

        def _commit(*_args: Any, **_kwargs: Any) -> MagicMock:
            return successor

        mixin.session_manager.update_chat_mode.side_effect = (
            lambda session_id, _mode: persist_targets.append(session_id)
        )

        with (
            patch(
                "gobby.servers.websocket.chat._session.commit_web_chat_clear_successor",
                side_effect=_commit,
            ),
            patch(
                "gobby.servers.websocket.chat._session.preserve_task_claim_state",
            ) as claims,
            patch(
                "gobby.servers.websocket.chat._session.SessionVariableManager",
            ) as sv_cls,
        ):
            sv_cls.return_value.get_variables.return_value = {}
            result = await mixin.commit_clear_successor(
                conversation_id="conv-1",
                session=session,
                predecessor_id="pred-db",
                attempt_id=ATTEMPT_ID,
            )

        assert result["ok"] is True
        assert result["successor_id"] == "succ-db"
        assert session.db_session_id == "succ-db"
        assert session.seq_num == 99
        assert session.message_index == 0
        assert mixin._ended == [("conv-1", SessionEndReason.CLEAR, "pred-db")]
        assert mixin.web_chat_runtime_manager.create_session.call_count == 0
        session._on_mode_persist("code")
        assert persist_targets == ["succ-db"]
        claims.assert_called_once()
        assert claims.call_args.args[2] == "succ-db"
        assert claims.call_args.args[3] == "pred-db"

    @pytest.mark.asyncio
    async def test_commit_transaction_failure_does_not_rebind(self) -> None:
        mixin = _CommitMixin()
        session = _live_session()
        mixin._chat_sessions["conv-1"] = session

        with patch(
            "gobby.servers.websocket.chat._session.commit_web_chat_clear_successor",
            return_value=None,
        ):
            result = await mixin.commit_clear_successor(
                conversation_id="conv-1",
                session=session,
                predecessor_id="pred-db",
                attempt_id=ATTEMPT_ID,
            )

        assert result["ok"] is False
        assert session.db_session_id == "pred-db"
        assert session.seq_num == 11
        assert mixin._ended == []


def _web_chat_row(sessions: SessionManager, project_id: str, label: str) -> Any:
    return sessions.create_web_chat_session(
        machine_id=LOCAL_MACHINE_ID,
        project_id=project_id,
        source="claude",
        sandbox_enabled=False,
        sandbox_policy_hash="policy-hash",
        title=label,
        chat_mode="normal",
        model="claude-opus",
    )


class TestCommitWebChatClearSuccessorTransaction:
    def test_one_transaction_expires_inserts_parents_and_seeds(self, hub_db: HubDatabase) -> None:
        project = LocalProjectManager(hub_db).create(
            name=f"clear-web-{uuid4().hex[:8]}",
            repo_path="/tmp/clear-web",
        )
        sessions = SessionManager(hub_db)
        predecessor = _web_chat_row(sessions, project.id, "predecessor")
        hub_db.execute(
            "UPDATE sessions SET handoff_markdown = %s WHERE id = %s",
            (HANDOFF, predecessor.id),
        )
        predecessor = sessions.get(predecessor.id)
        assert predecessor is not None
        stage_clear_attempt(
            hub_db,
            predecessor.id,
            attempt_id=ATTEMPT_ID,
            handoff_markdown=HANDOFF,
            observations=[],
            terminal_context=None,
            chat_context={"model": "claude-opus", "mode": "normal"},
        )

        successor = commit_web_chat_clear_successor(
            hub_db,
            predecessor_id=predecessor.id,
            attempt_id=ATTEMPT_ID,
        )

        assert successor is not None
        assert successor.id != predecessor.id
        refreshed_pred = sessions.get(predecessor.id)
        refreshed_succ = sessions.get(successor.id)
        assert refreshed_pred is not None
        assert refreshed_succ is not None
        assert refreshed_pred.status == "expired"
        assert refreshed_succ.status == "active"
        assert refreshed_succ.parent_session_id == predecessor.id
        assert refreshed_succ.session_type == "web_chat"
        assert refreshed_succ.external_id != predecessor.external_id
        assert refreshed_succ.external_id != predecessor.id
        marker = SessionVariableManager(hub_db).get_variables(predecessor.id)
        assert marker[CLEAR_ATTEMPT_VARIABLE]["consumed_by"] == successor.id
        seeded = SessionVariableManager(hub_db).get_variables(successor.id)
        assert seeded == {}
        assert refreshed_pred.handoff_markdown == HANDOFF

    def test_failed_insert_rolls_back_predecessor(self, hub_db: HubDatabase) -> None:
        project = LocalProjectManager(hub_db).create(
            name=f"clear-web-fail-{uuid4().hex[:8]}",
            repo_path="/tmp/clear-web-fail",
        )
        sessions = SessionManager(hub_db)
        predecessor = _web_chat_row(sessions, project.id, "predecessor")
        stage_clear_attempt(
            hub_db,
            predecessor.id,
            attempt_id=ATTEMPT_ID,
            handoff_markdown=HANDOFF,
            observations=[],
            terminal_context=None,
            chat_context=None,
        )
        conflict_id = str(uuid4())
        hub_db.execute(
            """
            INSERT INTO sessions (
                id, external_id, machine_id, source, project_id, session_type, status
            ) VALUES (%s, %s, %s, %s, %s, 'web_chat', 'active')
            """,
            (
                conflict_id,
                f"conflict-{conflict_id}",
                LOCAL_MACHINE_ID,
                "claude",
                project.id,
            ),
        )

        with patch("gobby.sessions.clear_continuation.uuid4", return_value=conflict_id):
            successor = commit_web_chat_clear_successor(
                hub_db,
                predecessor_id=predecessor.id,
                attempt_id=ATTEMPT_ID,
            )

        assert successor is None
        refreshed = sessions.get(predecessor.id)
        assert refreshed is not None
        assert refreshed.status == "active"
        marker = SessionVariableManager(hub_db).get_variables(predecessor.id)
        assert marker[CLEAR_ATTEMPT_VARIABLE]["consumed_by"] is None

    @pytest.mark.asyncio
    async def test_web_path_transfers_claims_before_returning(self, hub_db: HubDatabase) -> None:
        project = LocalProjectManager(hub_db).create(
            name=f"clear-web-claims-{uuid4().hex[:8]}",
            repo_path="/tmp/clear-web-claims",
        )
        sessions = SessionManager(hub_db)
        predecessor = _web_chat_row(sessions, project.id, "predecessor")
        third = sessions.register(
            external_id=f"third-{uuid4()}",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=project.id,
        )
        hub_db.execute(
            "UPDATE sessions SET handoff_markdown = %s WHERE id = %s",
            (HANDOFF, predecessor.id),
        )
        stage_clear_attempt(
            hub_db,
            predecessor.id,
            attempt_id=ATTEMPT_ID,
            handoff_markdown=HANDOFF,
            observations=[],
            terminal_context=None,
            chat_context=None,
        )
        task_manager = LocalTaskManager(hub_db)
        session_task_manager = SessionTaskManager(hub_db)
        sv_mgr = SessionVariableManager(hub_db)
        kept = task_manager.create_task(
            project.id,
            title="Stays with predecessor owner",
            validation_criteria="Observable close.",
        )
        stolen = task_manager.create_task(
            project.id,
            title="Moved to a third session",
            validation_criteria="Observable close.",
        )
        task_manager.claim_task(kept.id, session_id=predecessor.id)
        task_manager.claim_task(stolen.id, session_id=predecessor.id)
        session_task_manager.link_task(predecessor.id, kept.id, "claimed")
        session_task_manager.link_task(predecessor.id, stolen.id, "claimed")
        task_manager.claim_task(stolen.id, session_id=third.id, expected_owner=predecessor.id)
        sv_mgr.merge_variables(
            predecessor.id,
            {
                "task_claimed": True,
                "claimed_tasks": {
                    kept.id: f"#{kept.seq_num}",
                    stolen.id: f"#{stolen.seq_num}",
                },
                "session_had_task": True,
            },
        )

        mixin = _CommitMixin()
        mixin.session_manager = sessions
        mixin.event_handlers = MagicMock()
        mixin.event_handlers._task_manager = task_manager
        mixin.event_handlers._session_task_manager = session_task_manager
        session = _live_session(db_session_id=predecessor.id)
        mixin._chat_sessions["conv-1"] = session

        order: list[str] = []
        real_preserve = preserve_task_claim_state

        def _preserve(*args: Any, **kwargs: Any) -> None:
            order.append("claims")
            real_preserve(*args, **kwargs)

        with patch(
            "gobby.servers.websocket.chat._session.preserve_task_claim_state",
            side_effect=_preserve,
        ):
            result = await mixin.commit_clear_successor(
                conversation_id="conv-1",
                session=session,
                predecessor_id=predecessor.id,
                attempt_id=ATTEMPT_ID,
            )

        assert result["ok"] is True
        successor_id = result["successor_id"]
        assert successor_id != predecessor.id
        assert session.db_session_id == successor_id
        assert order == ["claims"]
        assert task_manager.get_task(kept.id).claimed_by_session_id == successor_id
        assert task_manager.get_task(stolen.id).claimed_by_session_id == third.id
        successor_vars = sv_mgr.get_variables(successor_id)
        assert successor_vars.get("task_claimed") is True
        assert successor_vars.get("claimed_tasks") == {kept.id: f"#{kept.seq_num}"}
        linked = {
            str(row["task"].id)
            for row in session_task_manager.get_session_tasks(successor_id)
            if row["action"] == "claimed"
        }
        assert kept.id in linked
        assert stolen.id not in linked
