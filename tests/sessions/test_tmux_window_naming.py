"""Tests for session tmux window naming and repair."""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.tmux.session_manager import TmuxReleaseOutcome
from gobby.sessions.tmux_window_naming import schedule_tmux_window_rename

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_scheduled_tmux_rename_is_retained_until_done() -> None:
    from gobby.hooks import background_tasks

    started = asyncio.Event()
    release = asyncio.Event()

    async def rename(_session: Any, _title: str) -> None:
        started.set()
        await release.wait()

    with patch("gobby.sessions.tmux_window_naming._rename_tmux_window", side_effect=rename):
        schedule_tmux_window_rename(MagicMock(), "title")

        await started.wait()
        assert len(background_tasks._background_tasks) == 1
        task = next(iter(background_tasks._background_tasks))
        callback_complete = asyncio.Event()
        task.add_done_callback(lambda _task: callback_complete.set())

        release.set()
        await callback_complete.wait()

        assert not background_tasks._background_tasks


class _RecordingTmuxManager:
    """Test double that records tmux config and rename calls."""

    instances: list[Any] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.rename_calls: list[tuple[str, str]] = []
        self.release_calls: list[str] = []
        self.fail = False
        self.instances.append(self)

    async def rename_window(self, target: str, title: str) -> bool:
        if self.fail:
            raise OSError("no tmux")
        self.rename_calls.append((target, title))
        return True

    async def release_window_title_ownership(self, target: str) -> TmuxReleaseOutcome:
        self.release_calls.append(target)
        return TmuxReleaseOutcome.RELEASED


class _ReloadingAppContext:
    def __init__(self, persisted_session: Any) -> None:
        self.persisted_session = persisted_session
        self.session_manager = SimpleNamespace(get=self._get_session)
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []

    def _get_session(self, _session_id: str) -> Any:
        return self.persisted_session

    async def run_db(self, func: Any, *args: Any) -> Any:
        self.calls.append((func, args))
        return func(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("loaded_candidates", [[], [SimpleNamespace(id="peer-session")]])
async def test_pane_ownership_keeps_requested_session_when_lookup_omits_it(
    loaded_candidates: list[Any],
) -> None:
    from gobby.sessions.tmux_window_naming import _resolve_tmux_pane_ownership

    session = SimpleNamespace(id="requested-session")
    container = SimpleNamespace(
        session_manager=SimpleNamespace(find_by_terminal_identity=MagicMock()),
        run_db=AsyncMock(return_value=loaded_candidates),
    )
    expected = MagicMock()

    with (
        patch("gobby.app_context.get_app_context", return_value=container),
        patch(
            "gobby.sessions.tmux_window_naming.terminal_session_identity",
            return_value=MagicMock(),
        ),
        patch(
            "gobby.sessions.tmux_window_naming.resolve_pane_ownership",
            return_value=expected,
        ) as resolve,
    ):
        result = await _resolve_tmux_pane_ownership(session)

    assert result is expected
    candidates = resolve.call_args.args[0]
    assert candidates[-1] is session


class TestRenameTmuxWindow:
    """Tests for _rename_tmux_window helper."""

    @pytest.mark.asyncio
    async def test_skips_when_no_terminal_context(self) -> None:
        """No-op when session has no terminal_context."""
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        session = MagicMock()
        session.terminal_context = None
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            await _rename_tmux_window(session, "Title")

        assert mock_exec.await_count == 0

    @pytest.mark.asyncio
    async def test_skips_when_no_tmux_pane(self) -> None:
        """No-op when terminal_context has no tmux_pane."""
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        session = MagicMock()
        session.terminal_context = {"parent_pid": 123}
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            await _rename_tmux_window(session, "Title")

        assert mock_exec.await_count == 0

    @pytest.mark.asyncio
    async def test_provisional_user_session_renames_on_default_server(self) -> None:
        """A provisional provider title is prefixed with the session ref."""
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42"}
        session.agent_depth = 0
        session.ref = "#99"
        session.title_source = "provisional"

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager):
            await _rename_tmux_window(session, "Codex")

        manager = _RecordingTmuxManager.instances[0]
        assert manager.config.socket_path is None
        assert manager.config.socket_name == ""
        assert manager.rename_calls == [("%42", "#99 Codex")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["active", "paused", "handoff_ready"])
    async def test_queued_rename_reloads_authoritative_persisted_title(self, status: str) -> None:
        from gobby.sessions.tmux_window_naming import _rename_tmux_window
        from gobby.terminal_ownership import PaneOwnershipDecision

        _RecordingTmuxManager.instances = []
        stale_session = SimpleNamespace(
            id="session-id",
            terminal_context={
                "tmux_pane": "%42",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
            agent_depth=0,
            ref="#99",
            title="#99 Codex",
        )
        persisted_session = SimpleNamespace(
            id="session-id",
            terminal_context={
                "tmux_pane": "%42",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
            agent_depth=0,
            ref="#99",
            title="Digest-owned title",
            status=status,
        )
        container = _ReloadingAppContext(persisted_session)
        ownership = PaneOwnershipDecision(
            identity=(
                "21000000-0000-4000-8000-000000000003",
                "tmux_socket_path:/tmp/tmux-501/default",
                "%42",
            ),
            requested_session_id="session-id",
            owner=persisted_session,
            reason="validated_foreground_process",
            validated_session_ids=frozenset({"session-id"}),
        )

        with (
            patch("gobby.app_context.get_app_context", return_value=container),
            patch(
                "gobby.sessions.tmux_window_naming._resolve_tmux_pane_ownership",
                return_value=ownership,
            ),
            patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager),
        ):
            await _rename_tmux_window(stale_session, "#99 Codex")

        assert container.calls == [(container.session_manager.get, ("session-id",))]
        manager = _RecordingTmuxManager.instances[0]
        assert manager.rename_calls == [("%42", "#99 Digest-owned title")]

    @pytest.mark.asyncio
    async def test_nested_child_queued_rename_is_rejected_for_parent_owned_pane(
        self,
    ) -> None:
        from gobby.sessions.tmux_window_naming import _rename_tmux_window
        from gobby.terminal_ownership import PaneOwnershipDecision

        terminal_context = {
            "tmux_pane": "%226",
            "tmux_socket_path": "/tmp/tmux-501/gobby",
        }
        parent = SimpleNamespace(
            id="codex-parent",
            terminal_context=terminal_context,
            status="active",
            title="Codex title",
        )
        child = SimpleNamespace(
            id="grok-child",
            terminal_context=terminal_context,
            status="paused",
            title="Grok title",
        )
        session_manager = SimpleNamespace(
            get=lambda _session_id: child,
            find_by_terminal_identity=lambda _identity: [child, parent],
        )

        async def run_db(func: Any, *args: Any) -> Any:
            return func(*args)

        container = SimpleNamespace(session_manager=session_manager, run_db=run_db)
        ownership = PaneOwnershipDecision(
            identity=(
                "21000000-0000-4000-8000-000000000003",
                "tmux_socket_path:/tmp/tmux-501/gobby",
                "%226",
            ),
            requested_session_id="grok-child",
            owner=parent,
            reason="nested_outermost_process",
            validated_session_ids=frozenset({"codex-parent", "grok-child"}),
        )
        _RecordingTmuxManager.instances = []

        with (
            patch("gobby.app_context.get_app_context", return_value=container),
            patch(
                "gobby.sessions.tmux_window_naming.resolve_pane_ownership",
                return_value=ownership,
            ),
            patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager),
        ):
            await _rename_tmux_window(child, "Grok title")

        assert _RecordingTmuxManager.instances == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["expired", "deleted"])
    async def test_queued_rename_skips_persisted_ineligible_session(
        self, status: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        stale_session = SimpleNamespace(
            id="session-id",
            terminal_context={"tmux_pane": "%42"},
            agent_depth=0,
            ref="#99",
            title="Queued title",
        )
        persisted_session = SimpleNamespace(
            id="session-id",
            terminal_context={"tmux_pane": "%42"},
            agent_depth=0,
            ref="#99",
            title="Late digest title",
            status=status,
        )
        container = _ReloadingAppContext(persisted_session)

        with (
            caplog.at_level(logging.WARNING, logger="gobby.sessions.tmux_window_naming"),
            patch("gobby.app_context.get_app_context", return_value=container),
            patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager),
        ):
            await _rename_tmux_window(stale_session, "Queued title")

        assert container.calls == [(container.session_manager.get, ("session-id",))]
        assert _RecordingTmuxManager.instances == []
        assert not caplog.records

    @pytest.mark.asyncio
    async def test_successful_window_rename_log_is_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42"}
        session.agent_depth = 0
        session.ref = "#99"

        with (
            caplog.at_level(logging.DEBUG, logger="gobby.sessions.tmux_window_naming"),
            patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager),
        ):
            await _rename_tmux_window(session, "My Title")

        rename_records = [
            record
            for record in caplog.records
            if record.getMessage().startswith("Renamed tmux window")
        ]
        assert len(rename_records) == 1
        assert rename_records[0].levelno == logging.DEBUG

    @pytest.mark.asyncio
    async def test_false_window_rename_result_logs_debug_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.sessions.tmux_window_naming import _apply_window_rename

        class MissingTargetTmuxManager(_RecordingTmuxManager):
            async def rename_window(self, target: str, title: str) -> bool:
                return False

        session = SimpleNamespace(agent_depth=0, ref="#99", status="active")

        with (
            caplog.at_level(logging.DEBUG, logger="gobby.sessions.tmux_window_naming"),
            patch("gobby.sessions.tmux_context.TmuxSessionManager", MissingTargetTmuxManager),
        ):
            applied = await _apply_window_rename(
                session,
                {"tmux_pane": "%42"},
                "%42",
                "My Title",
            )

        outcome_records = [
            record
            for record in caplog.records
            if record.getMessage().startswith("tmux window rename did not apply")
        ]
        assert applied is False
        assert len(outcome_records) == 1
        assert outcome_records[0].levelno == logging.DEBUG
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_empty_title_falls_back_to_source_not_cwd_basename(self) -> None:
        """Empty titles never use the cwd basename (the old ``#N gobby`` bug).

        The fallback is the session ``source``, even when a cwd basename exists —
        a path basename is indistinguishable from a real title.
        """
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42", "cwd": "/work/repos/gobby/"}
        session.agent_depth = 0
        session.ref = "#99"
        session.source = "claude"

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager):
            await _rename_tmux_window(session, "")

        manager = _RecordingTmuxManager.instances[0]
        assert manager.rename_calls == [("%42", "#99 claude")]

    @pytest.mark.asyncio
    async def test_unresolved_session_ref_uses_seq_num(self) -> None:
        """The literal #session_ref placeholder is never used in a window title."""
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42", "cwd": "/work/repos/gobby/"}
        session.agent_depth = 0
        session.ref = "#session_ref"
        session.seq_num = 99
        session.source = "claude"

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager):
            await _rename_tmux_window(session, "")

        manager = _RecordingTmuxManager.instances[0]
        assert manager.rename_calls == [("%42", "#99 claude")]

    def test_unresolved_session_ref_detection_requires_placeholder_token(self) -> None:
        from gobby.sessions.tmux_window_naming import _contains_unresolved_session_ref

        assert _contains_unresolved_session_ref("#session_ref gobby") is True
        assert _contains_unresolved_session_ref("#{session_ref}: gobby") is True
        assert _contains_unresolved_session_ref("{session_ref}: gobby") is True
        assert _contains_unresolved_session_ref("session_ref: gobby") is False
        assert _contains_unresolved_session_ref("session_reference: gobby") is False

    def test_resolve_window_title_does_not_duplicate_existing_ref_prefix(self) -> None:
        from gobby.sessions.tmux_window_naming import _resolve_window_title

        session = MagicMock()
        session.ref = "#99"

        assert _resolve_window_title(session, {}, "#99: My Title") == "#99 My Title"
        assert _resolve_window_title(session, {}, "  #99: My Title") == "#99 My Title"
        assert _resolve_window_title(session, {}, "#99 codex") == "#99 codex"
        assert _resolve_window_title(session, {}, "#99: #99 codex") == "#99 codex"

    def test_resolve_window_title_sanitizes_command_titles(self) -> None:
        from gobby.sessions.tmux_window_naming import _resolve_window_title

        session = MagicMock()
        session.ref = "#99"
        session.source = "codex"

        assert _resolve_window_title(session, {}, "$gobby coderabbit") == "#99 codex"
        assert _resolve_window_title(session, {}, "/gobby coderabbit") == "#99 codex"
        assert (
            _resolve_window_title(session, {}, "#99: $gobby coderabbit fix review comments")
            == "#99 Fix review comments"
        )
        assert _resolve_window_title(session, {}, "Fix bug: logs") == "#99 Fix bug - logs"

    @pytest.mark.asyncio
    async def test_unresolved_title_falls_back_before_prefixing(self) -> None:
        """A stored placeholder title is replaced with the source fallback."""
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42", "cwd": "/work/repos/gobby/"}
        session.agent_depth = 0
        session.ref = "#session_ref"
        session.seq_num = 99
        session.source = "claude"

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager):
            await _rename_tmux_window(session, "#session_ref gobby")

        manager = _RecordingTmuxManager.instances[0]
        assert manager.rename_calls == [("%42", "#99 claude")]

    @pytest.mark.asyncio
    async def test_empty_title_falls_back_to_source_then_untitled(self) -> None:
        """Empty titles use the session source, then a neutral 'untitled' label."""
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        source_session = MagicMock()
        source_session.terminal_context = {"tmux_pane": "%43", "cwd": "/work/repos/gobby"}
        source_session.agent_depth = 0
        source_session.ref = None
        source_session.source = "codex"

        session_fallback = MagicMock()
        session_fallback.terminal_context = {"tmux_pane": "%44", "cwd": "/work/repos/gobby"}
        session_fallback.agent_depth = 0
        session_fallback.ref = None
        session_fallback.source = None

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager):
            await _rename_tmux_window(source_session, "")
            await _rename_tmux_window(session_fallback, "")

        # Even with a cwd basename present, the fallback is source / "untitled" —
        # never the directory name.
        assert _RecordingTmuxManager.instances[0].rename_calls == [("%43", "codex")]
        assert _RecordingTmuxManager.instances[1].rename_calls == [("%44", "untitled")]

    @pytest.mark.asyncio
    async def test_spawned_agent_renames_on_gobby_socket(self) -> None:
        """Spawned agent (depth > 0) uses TmuxSessionManager."""
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%0"}
        session.agent_depth = 1
        session.ref = "#55"

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager):
            await _rename_tmux_window(session, "Agent Title")

        manager = _RecordingTmuxManager.instances[0]
        assert manager.config.socket_path is None
        assert manager.config.socket_name == "gobby"
        assert manager.rename_calls == [("%0", "#55 Agent Title")]

    @pytest.mark.asyncio
    async def test_tmux_socket_path_overrides_socket_name(self) -> None:
        """Stored tmux_socket_path routes renames to that exact server."""
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        session = MagicMock()
        session.terminal_context = {
            "tmux_pane": "%9",
            "tmux_socket_path": "/tmp/tmux-501/gobby",
            "tmux_socket_name": "ignored",
        }
        session.agent_depth = 0
        session.ref = None

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager):
            await _rename_tmux_window(session, "Socket Path Title")

        manager = _RecordingTmuxManager.instances[0]
        assert manager.config.socket_path == "/tmp/tmux-501/gobby"
        assert manager.config.socket_name == ""
        assert manager.rename_calls == [("%9", "Socket Path Title")]

    @pytest.mark.asyncio
    async def test_tmux_socket_name_routes_to_named_server(self) -> None:
        """Stored tmux_socket_name routes renames when no path is present."""
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%10", "tmux_socket_name": "gobby"}
        session.agent_depth = 0
        session.ref = None

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager):
            await _rename_tmux_window(session, "Named Socket Title")

        manager = _RecordingTmuxManager.instances[0]
        assert manager.config.socket_path is None
        assert manager.config.socket_name == "gobby"
        assert manager.rename_calls == [("%10", "Named Socket Title")]

    @pytest.mark.asyncio
    async def test_failure_does_not_propagate_and_logs_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Rename failures are visible but never propagated."""
        from gobby.sessions.tmux_window_naming import _rename_tmux_window

        _RecordingTmuxManager.instances = []
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42"}
        session.agent_depth = 0
        session.ref = "#99"

        class FailingTmuxManager(_RecordingTmuxManager):
            async def rename_window(self, target: str, title: str) -> bool:
                raise OSError("no tmux")

        with (
            caplog.at_level(logging.DEBUG, logger="gobby.sessions.tmux_window_naming"),
            patch("gobby.sessions.tmux_context.TmuxSessionManager", FailingTmuxManager),
        ):
            await _rename_tmux_window(session, "Title")

        assert "tmux window rename errored" in caplog.text
        assert "#99 pane=%42 socket=default" in caplog.text
        assert "title=" not in caplog.text
        assert any(record.levelno == logging.WARNING for record in caplog.records)


# =============================================================================
# Tests for enforce_window_name_if_unmanaged (repair-sweep helper)
# =============================================================================


class _EnforceTmuxManager:
    """Records rename calls and returns a configurable automatic-rename flag."""

    instances: list["_EnforceTmuxManager"] = []
    auto_rename_return: bool | None = True
    window_name_return: str | None = None

    def __init__(self, config: Any) -> None:
        self.config = config
        self.rename_calls: list[tuple[str, str]] = []
        _EnforceTmuxManager.instances.append(self)

    async def get_window_automatic_rename(self, target: str) -> bool | None:
        return type(self).auto_rename_return

    async def get_window_name(self, target: str) -> str | None:
        return type(self).window_name_return

    async def rename_window(self, target: str, title: str) -> bool:
        self.rename_calls.append((target, title))
        return True


class TestEnforceWindowNameIfUnmanaged:
    """Tests for the periodic repair-sweep rename helper."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["expired", "handoff_ready"])
    async def test_skips_persisted_ineligible_session(self, status: str) -> None:
        from gobby.sessions.tmux_window_naming import enforce_window_name_if_unmanaged

        _EnforceTmuxManager.instances = []
        stale_session = SimpleNamespace(id="session-id")
        persisted_session = SimpleNamespace(
            id="session-id",
            terminal_context={"tmux_pane": "%42"},
            agent_depth=0,
            ref="#99",
            title="Historical title",
            status=status,
        )
        container = _ReloadingAppContext(persisted_session)

        with (
            patch("gobby.app_context.get_app_context", return_value=container),
            patch("gobby.sessions.tmux_context.TmuxSessionManager", _EnforceTmuxManager),
        ):
            acted = await enforce_window_name_if_unmanaged(stale_session)

        assert acted is False
        assert container.calls == [(container.session_manager.get, ("session-id",))]
        assert _EnforceTmuxManager.instances == []

    @pytest.mark.asyncio
    async def test_renames_unmanaged_window_with_fallback(self) -> None:
        """An un-named window (automatic-rename on) is renamed using the fallback."""
        from gobby.sessions.tmux_window_naming import enforce_window_name_if_unmanaged

        _EnforceTmuxManager.instances = []
        _EnforceTmuxManager.auto_rename_return = True
        _EnforceTmuxManager.window_name_return = None
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42", "cwd": "/work/repos/gobby/"}
        session.agent_depth = 0
        session.ref = "#99"
        session.title = ""
        session.source = "claude"

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _EnforceTmuxManager):
            acted = await enforce_window_name_if_unmanaged(session)

        assert acted is True
        rename_calls = [c for m in _EnforceTmuxManager.instances for c in m.rename_calls]
        assert rename_calls == [("%42", "#99 claude")]

    @pytest.mark.asyncio
    async def test_skips_window_already_managed(self) -> None:
        """A managed window matching the persisted title is left untouched."""
        from gobby.sessions.tmux_window_naming import enforce_window_name_if_unmanaged

        _EnforceTmuxManager.instances = []
        _EnforceTmuxManager.auto_rename_return = False
        _EnforceTmuxManager.window_name_return = "#99 Session title"
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42", "cwd": "/work/repos/gobby/"}
        session.agent_depth = 0
        session.ref = "#99"
        session.title = "Session title"

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _EnforceTmuxManager):
            acted = await enforce_window_name_if_unmanaged(session)

        assert acted is False
        rename_calls = [c for m in _EnforceTmuxManager.instances for c in m.rename_calls]
        assert rename_calls == []

    @pytest.mark.asyncio
    async def test_repairs_managed_window_with_stale_persisted_title(self) -> None:
        """A managed window follows a newer persisted session title."""
        from gobby.sessions.tmux_window_naming import enforce_window_name_if_unmanaged

        _EnforceTmuxManager.instances = []
        _EnforceTmuxManager.auto_rename_return = False
        _EnforceTmuxManager.window_name_return = "#99 Old title"
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42"}
        session.agent_depth = 0
        session.ref = "#99"
        session.title = "New title"

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _EnforceTmuxManager):
            acted = await enforce_window_name_if_unmanaged(session)

        assert acted is True
        rename_calls = [c for m in _EnforceTmuxManager.instances for c in m.rename_calls]
        assert rename_calls == [("%42", "#99 New title")]

    @pytest.mark.asyncio
    async def test_repairs_managed_window_with_unresolved_placeholder(self) -> None:
        """A previously managed bad name is repaired instead of skipped."""
        from gobby.sessions.tmux_window_naming import enforce_window_name_if_unmanaged

        _EnforceTmuxManager.instances = []
        _EnforceTmuxManager.auto_rename_return = False
        _EnforceTmuxManager.window_name_return = "#session_ref gobby"
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42", "cwd": "/work/repos/gobby/"}
        session.agent_depth = 0
        session.ref = "#session_ref"
        session.seq_num = 99
        session.title = "#session_ref gobby"
        session.source = "claude"

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _EnforceTmuxManager):
            acted = await enforce_window_name_if_unmanaged(session)

        assert acted is True
        rename_calls = [c for m in _EnforceTmuxManager.instances for c in m.rename_calls]
        assert rename_calls == [("%42", "#99 claude")]

    @pytest.mark.asyncio
    async def test_repairs_managed_window_with_duplicated_provisional_ref(self) -> None:
        from gobby.sessions.tmux_window_naming import enforce_window_name_if_unmanaged

        _EnforceTmuxManager.instances = []
        _EnforceTmuxManager.auto_rename_return = False
        _EnforceTmuxManager.window_name_return = "#99: #99 codex"
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42"}
        session.agent_depth = 0
        session.ref = "#99"
        session.title = "#99 codex"
        session.source = "codex"

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _EnforceTmuxManager):
            acted = await enforce_window_name_if_unmanaged(session)

        assert acted is True
        rename_calls = [c for m in _EnforceTmuxManager.instances for c in m.rename_calls]
        assert rename_calls == [("%42", "#99 codex")]

    @pytest.mark.asyncio
    async def test_skips_when_window_unreadable(self) -> None:
        """A vanished window (automatic-rename None) is not renamed."""
        from gobby.sessions.tmux_window_naming import enforce_window_name_if_unmanaged

        _EnforceTmuxManager.instances = []
        _EnforceTmuxManager.auto_rename_return = None
        _EnforceTmuxManager.window_name_return = "#session_ref gobby"
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%42", "cwd": "/x/gobby"}
        session.agent_depth = 0
        session.ref = "#99"
        session.title = ""

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _EnforceTmuxManager):
            acted = await enforce_window_name_if_unmanaged(session)

        assert acted is False
        assert all(not m.rename_calls for m in _EnforceTmuxManager.instances)

    @pytest.mark.asyncio
    async def test_skips_when_no_tmux_pane(self) -> None:
        """No tmux_pane in terminal_context -> no tmux work at all."""
        from gobby.sessions.tmux_window_naming import enforce_window_name_if_unmanaged

        _EnforceTmuxManager.instances = []
        session = MagicMock()
        session.terminal_context = {"cwd": "/x"}

        with patch("gobby.sessions.tmux_context.TmuxSessionManager", _EnforceTmuxManager):
            acted = await enforce_window_name_if_unmanaged(session)

        assert acted is False
        assert _EnforceTmuxManager.instances == []


class TestReleaseWindowNameIfUnowned:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["expired", "deleted"])
    async def test_releases_inactive_ownerless_pane(self, status: str) -> None:
        from gobby.sessions.tmux_window_naming import release_window_name_if_unowned
        from gobby.terminal_ownership import PaneOwnershipDecision

        _RecordingTmuxManager.instances = []
        session = SimpleNamespace(
            id="stale-session",
            status=status,
            title="#7951 Frozen title",
            terminal_context={
                "tmux_pane": "%42",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
        )
        ownership = PaneOwnershipDecision(
            identity=(
                "21000000-0000-4000-8000-000000000003",
                "tmux_socket_path:/tmp/tmux-501/default",
                "%42",
            ),
            requested_session_id="stale-session",
            owner=None,
            reason="ownerless",
        )

        with (
            patch(
                "gobby.sessions.tmux_window_naming._reload_persisted_session",
                return_value=session,
            ),
            patch(
                "gobby.sessions.tmux_window_naming._resolve_tmux_pane_ownership",
                return_value=ownership,
            ),
            patch("gobby.sessions.tmux_context.TmuxSessionManager", _RecordingTmuxManager),
        ):
            released = await release_window_name_if_unowned(session)

        assert released is True
        assert _RecordingTmuxManager.instances[0].release_calls == ["%42"]

    @pytest.mark.asyncio
    async def test_preserves_title_when_foreground_owner_exists(self) -> None:
        from gobby.sessions.tmux_window_naming import release_window_name_if_unowned
        from gobby.terminal_ownership import PaneOwnershipDecision

        inactive = SimpleNamespace(
            id="stale-session",
            status="expired",
            terminal_context={"tmux_pane": "%42"},
        )
        active = SimpleNamespace(id="active-session", status="active")
        ownership = PaneOwnershipDecision(
            identity=("21000000-0000-4000-8000-000000000003", "tmux_socket_name:gobby", "%42"),
            requested_session_id="stale-session",
            owner=active,
            reason="validated_foreground_process",
        )

        with (
            patch(
                "gobby.sessions.tmux_window_naming._reload_persisted_session",
                return_value=inactive,
            ),
            patch(
                "gobby.sessions.tmux_window_naming._resolve_tmux_pane_ownership",
                return_value=ownership,
            ),
        ):
            released = await release_window_name_if_unowned(inactive)

        assert released is False


class TestSynthesizeFallbackTitle:
    """Tests that the empty-title fallback never leaks a directory basename."""

    def test_never_uses_path_basename(self) -> None:
        from gobby.sessions.tmux_window_naming import _synthesize_fallback_title

        session = MagicMock()
        session.source = "claude"
        terminal_context = {
            "cwd": "/work/repos/gobby/",
            "project_path": "/work/repos/gobby",
            "workspace_path": "/work/repos/gobby",
            "repo_path": "/work/repos/gobby",
        }

        # The directory name 'gobby' must never surface as a title.
        assert _synthesize_fallback_title(session, terminal_context) == "claude"

    def test_falls_back_to_untitled_without_source(self) -> None:
        from gobby.sessions.tmux_window_naming import _synthesize_fallback_title

        session = MagicMock()
        session.source = None

        assert _synthesize_fallback_title(session, {"cwd": "/work/repos/gobby"}) == "untitled"
