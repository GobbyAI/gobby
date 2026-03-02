"""Unit tests for the gobby.agents.tmux module.

Tests session manager, output reader, config, errors, and singletons.
All tmux subprocess calls are mocked — no real tmux binary required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from gobby.agents.tmux.config import TmuxConfig
from gobby.agents.tmux.errors import TmuxNotFoundError, TmuxSessionError
from gobby.agents.tmux.output_reader import TmuxOutputReader
from gobby.agents.tmux.pty_bridge import TmuxPTYBridge
from gobby.agents.tmux.session_manager import TmuxSessionInfo, TmuxSessionManager
from gobby.agents.tmux.spawner import TmuxSpawner
from gobby.config.tmux import TmuxConfig as TmuxConfigCanonical

pytestmark = pytest.mark.unit


# =============================================================================
# TmuxConfig
# =============================================================================


class TestTmuxConfig:
    """Tests for TmuxConfig pydantic model."""

    def test_defaults(self) -> None:
        config = TmuxConfig()
        assert config.enabled is True
        assert config.command == "tmux"
        assert config.socket_name == "gobby"
        assert config.config_file is None
        assert config.session_prefix == "gobby"
        assert config.history_limit == 10000

    def test_custom_values(self) -> None:
        config = TmuxConfig(
            enabled=False,
            command="/usr/local/bin/tmux",
            socket_name="test",
            config_file="/tmp/tmux.conf",
            session_prefix="myprefix",
            history_limit=5000,
        )
        assert config.enabled is False
        assert config.command == "/usr/local/bin/tmux"
        assert config.socket_name == "test"
        assert config.config_file == "/tmp/tmux.conf"
        assert config.session_prefix == "myprefix"
        assert config.history_limit == 5000

    def test_wsl_distribution_default(self) -> None:
        config = TmuxConfig()
        assert config.wsl_distribution is None

    def test_wsl_distribution_custom(self) -> None:
        config = TmuxConfig(wsl_distribution="Ubuntu")
        assert config.wsl_distribution == "Ubuntu"

    def test_history_limit_minimum(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal to 100"):
            TmuxConfig(history_limit=50)

    def test_re_export_matches_canonical(self) -> None:
        """agents/tmux/config.py re-exports the same class from config/tmux.py."""
        assert TmuxConfig is TmuxConfigCanonical


# =============================================================================
# Errors
# =============================================================================


class TestTmuxErrors:
    """Tests for TmuxNotFoundError and TmuxSessionError."""

    def test_not_found_error_default(self) -> None:
        err = TmuxNotFoundError()
        assert "tmux" in str(err)
        assert "not found" in str(err)
        assert err.command == "tmux"

    def test_not_found_error_has_install_hint(self) -> None:
        err = TmuxNotFoundError()
        msg = str(err)
        # Should contain platform-specific install instructions
        assert "Install" in msg or "install" in msg

    def test_not_found_error_custom_command(self) -> None:
        err = TmuxNotFoundError("/opt/tmux")
        assert "/opt/tmux" in str(err)
        assert err.command == "/opt/tmux"

    def test_session_error_with_name(self) -> None:
        err = TmuxSessionError("already exists", session_name="test")
        assert "test" in str(err)
        assert "already exists" in str(err)
        assert err.session_name == "test"

    def test_session_error_without_name(self) -> None:
        err = TmuxSessionError("generic failure")
        assert "tmux:" in str(err)
        assert err.session_name is None


# =============================================================================
# TmuxSessionManager
# =============================================================================


class TestTmuxSessionManager:
    """Tests for TmuxSessionManager."""

    def test_base_args_default(self) -> None:
        mgr = TmuxSessionManager()
        args = mgr._base_args()
        assert args == ["tmux", "-L", "gobby"]

    def test_base_args_with_config_file(self) -> None:
        config = TmuxConfig(config_file="/tmp/my.conf", socket_name="test")
        mgr = TmuxSessionManager(config)
        args = mgr._base_args()
        assert args == ["tmux", "-L", "test", "-f", "/tmp/my.conf"]

    def test_base_args_empty_socket_name(self) -> None:
        """Empty socket_name skips -L flag (uses default tmux server)."""
        config = TmuxConfig(socket_name="")
        mgr = TmuxSessionManager(config)
        args = mgr._base_args()
        assert args == ["tmux"]

    def test_base_args_empty_socket_with_config(self) -> None:
        config = TmuxConfig(socket_name="", config_file="/tmp/my.conf")
        mgr = TmuxSessionManager(config)
        args = mgr._base_args()
        assert args == ["tmux", "-f", "/tmp/my.conf"]

    @patch("shutil.which", return_value="/usr/bin/tmux")
    def test_is_available_true(self, mock_which) -> None:
        mgr = TmuxSessionManager()
        assert mgr.is_available() is True

    @patch("shutil.which", return_value=None)
    def test_is_available_false(self, mock_which) -> None:
        mgr = TmuxSessionManager()
        assert mgr.is_available() is False

    @patch("shutil.which", return_value=None)
    def test_require_available_raises(self, mock_which) -> None:
        mgr = TmuxSessionManager()
        with pytest.raises(TmuxNotFoundError):
            mgr.require_available()

    @pytest.mark.asyncio
    async def test_create_session_success(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            # create_session calls _run three times: has_session, new-session, display-message (get_pane_pid)
            mock_run.side_effect = [
                (1, "", ""),  # has_session → not found (rc=1)
                (0, "", ""),  # new-session
                (0, "12345\n", ""),  # display-message for pane_pid
            ]
            with patch.object(mgr, "is_available", return_value=True):
                info = await mgr.create_session(
                    name="test.session:1",
                    command="echo hello",
                    cwd="/tmp",
                )

            assert info.name == "test-session-1"  # sanitised
            assert info.pane_pid == 12345

    @pytest.mark.asyncio
    async def test_create_session_failure(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "duplicate session: test")
            with (
                patch.object(mgr, "is_available", return_value=True),
                pytest.raises(TmuxSessionError, match="duplicate session"),
            ):
                await mgr.create_session(name="test")

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no server running")
            result = await mgr.list_sessions()
            assert result == []

    @pytest.mark.asyncio
    async def test_list_sessions_with_entries(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            # Format: session_name\tpane_pid\tpane_id\twindow_name\tpane_title\tpane_dead
            mock_run.return_value = (
                0,
                "session1\t100\t%1\tzsh\t\t0\nsession2\t200\t%2\tzsh\t\t0\n",
                "",
            )
            result = await mgr.list_sessions()
            assert len(result) == 2
            assert result[0].name == "session1"
            assert result[0].pane_pid == 100
            assert result[0].pane_id == "%1"
            assert result[1].name == "session2"
            assert result[1].pane_id == "%2"

    @pytest.mark.asyncio
    async def test_has_session(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            assert await mgr.has_session("test") is True

            mock_run.return_value = (1, "", "")
            assert await mgr.has_session("missing") is False

    @pytest.mark.asyncio
    async def test_kill_session(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            assert await mgr.kill_session("test") is True

            mock_run.return_value = (1, "", "no such session")
            assert await mgr.kill_session("missing") is False

    @pytest.mark.asyncio
    async def test_rename_window_success(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            assert await mgr.rename_window("%42", "My Title") is True
            mock_run.assert_called_once_with(
                "set-option",
                "-g",
                "set-titles",
                "on",
                ";",
                "set-option",
                "-g",
                "set-titles-string",
                "#W",
                ";",
                "rename-window",
                "-t",
                "%42",
                "My Title",
                ";",
                "set-option",
                "-w",
                "-t",
                "%42",
                "automatic-rename",
                "off",
            )

    @pytest.mark.asyncio
    async def test_rename_window_failure(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (1, "", "no such window")
            assert await mgr.rename_window("%99", "Title") is False

    @pytest.mark.asyncio
    async def test_send_keys(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "", "")
            assert await mgr.send_keys("test", "hello") is True

    @pytest.mark.asyncio
    async def test_get_pane_pid(self) -> None:
        mgr = TmuxSessionManager()
        with patch.object(mgr, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (0, "42\n", "")
            assert await mgr.get_pane_pid("test") == 42

            mock_run.return_value = (1, "", "")
            assert await mgr.get_pane_pid("missing") is None


# =============================================================================
# TmuxOutputReader
# =============================================================================


class TestTmuxOutputReader:
    """Tests for TmuxOutputReader."""

    def test_set_output_callback(self) -> None:
        reader = TmuxOutputReader()
        assert reader._output_callback is None

        async def cb(run_id: str, data: str) -> None:
            pass

        reader.set_output_callback(cb)
        assert reader._output_callback is cb

    def test_base_args(self) -> None:
        config = TmuxConfig(socket_name="test-sock")
        reader = TmuxOutputReader(config)
        args = reader._base_args()
        assert args == ["tmux", "-L", "test-sock"]

    def test_base_args_empty_socket(self) -> None:
        """Empty socket_name skips -L flag."""
        config = TmuxConfig(socket_name="")
        reader = TmuxOutputReader(config)
        args = reader._base_args()
        assert args == ["tmux"]

    @pytest.mark.asyncio
    async def test_stop_reader_not_running(self) -> None:
        reader = TmuxOutputReader()
        assert await reader.stop_reader("nonexistent") is False

    @pytest.mark.asyncio
    async def test_stop_all_empty(self) -> None:
        reader = TmuxOutputReader()
        await reader.stop_all()  # should not raise


# =============================================================================
# TmuxSessionInfo
# =============================================================================


class TestTmuxSessionInfo:
    def test_defaults(self) -> None:
        info = TmuxSessionInfo(name="test")
        assert info.name == "test"
        assert info.pane_pid is None
        assert info.pane_id is None
        assert info.window_name is None
        assert info.created_at > 0


# =============================================================================
# Singletons
# =============================================================================


class TestSingletons:
    """Tests for module-level singleton getters."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self) -> None:
        """Reset module-level singletons before and after each test."""
        import gobby.agents.tmux as mod

        mod._session_manager = None
        mod._output_reader = None
        yield
        mod._session_manager = None
        mod._output_reader = None

    def test_get_tmux_session_manager_returns_same(self) -> None:
        import gobby.agents.tmux as mod

        mgr1 = mod.get_tmux_session_manager()
        mgr2 = mod.get_tmux_session_manager()
        assert mgr1 is mgr2

    def test_get_tmux_output_reader_returns_same(self) -> None:
        import gobby.agents.tmux as mod

        r1 = mod.get_tmux_output_reader()
        r2 = mod.get_tmux_output_reader()
        assert r1 is r2


# =============================================================================
# DaemonConfig integration
# =============================================================================


class TestDaemonConfigTmux:
    """TmuxConfig is properly wired into DaemonConfig."""

    def test_default_tmux_config(self) -> None:
        from gobby.config.app import DaemonConfig

        config = DaemonConfig()
        assert config.tmux.enabled is True
        assert config.tmux.socket_name == "gobby"

    def test_custom_tmux_config(self) -> None:
        from gobby.config.app import DaemonConfig

        config = DaemonConfig(tmux={"enabled": False, "socket_name": "custom"})
        assert config.tmux.enabled is False
        assert config.tmux.socket_name == "custom"


# =============================================================================
# TmuxPTYBridge
# =============================================================================


class TestTmuxPTYBridge:
    """Tests for TmuxPTYBridge."""

    @pytest.mark.asyncio
    async def test_init(self) -> None:
        bridge = TmuxPTYBridge()
        assert bridge._bridges == {}
        assert await bridge.list_bridges() == {}

    @pytest.mark.asyncio
    async def test_get_master_fd_missing(self) -> None:
        bridge = TmuxPTYBridge()
        assert await bridge.get_master_fd("nonexistent") is None

    @pytest.mark.asyncio
    async def test_get_bridge_missing(self) -> None:
        bridge = TmuxPTYBridge()
        assert await bridge.get_bridge("nonexistent") is None

    def test_build_attach_cmd_gobby(self) -> None:
        bridge = TmuxPTYBridge()
        config = TmuxConfig(socket_name="gobby")
        cmd = bridge._build_attach_cmd("my-session", config)
        assert cmd == ["tmux", "-L", "gobby", "attach-session", "-t", "my-session"]

    def test_build_attach_cmd_default_server(self) -> None:
        bridge = TmuxPTYBridge()
        config = TmuxConfig(socket_name="")
        cmd = bridge._build_attach_cmd("my-session", config)
        assert cmd == ["tmux", "attach-session", "-t", "my-session"]

    @pytest.mark.asyncio
    async def test_detach_missing_is_noop(self) -> None:
        bridge = TmuxPTYBridge()
        await bridge.detach("nonexistent")  # should not raise

    @pytest.mark.asyncio
    async def test_detach_all_empty(self) -> None:
        bridge = TmuxPTYBridge()
        await bridge.detach_all()  # should not raise

    @pytest.mark.asyncio
    async def test_attach_duplicate_raises(self) -> None:
        bridge = TmuxPTYBridge()
        # Manually insert a bridge entry
        from unittest.mock import MagicMock

        from gobby.agents.tmux.pty_bridge import BridgeInfo

        mock_proc = MagicMock()
        bridge._bridges["test-id"] = BridgeInfo(
            master_fd=999, proc=mock_proc, session_name="sess", socket_name="gobby"
        )

        with pytest.raises(RuntimeError, match="already exists"):
            await bridge.attach("sess", "test-id")

    @pytest.mark.asyncio
    async def test_resize_missing_is_noop(self) -> None:
        bridge = TmuxPTYBridge()
        await bridge.resize("nonexistent", 50, 200)  # should not raise


# =============================================================================
# TmuxSpawner
# =============================================================================


class TestTmuxSpawner:
    """Tests for TmuxSpawner._async_spawn environment handling."""

    @pytest.mark.asyncio
    async def test_virtual_env_cleared_in_extra_env(self) -> None:
        """VIRTUAL_ENV and VIRTUAL_ENV_PROMPT are set to empty via -e flags."""
        spawner = TmuxSpawner()
        with patch.object(
            spawner._session_manager, "create_session", new_callable=AsyncMock
        ) as mock_create:
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(
                command=["echo", "hello"],
                cwd="/tmp",
                env={"GOBBY_SESSION_ID": "sess-1"},
            )

            mock_create.assert_called_once()
            env_arg = (
                mock_create.call_args[1].get("env") or mock_create.call_args[0][3]
                if len(mock_create.call_args[0]) > 3
                else mock_create.call_args[1].get("env")
            )
            assert env_arg["VIRTUAL_ENV"] == ""
            assert env_arg["VIRTUAL_ENV_PROMPT"] == ""
            # Gobby-specific vars should still be passed
            assert env_arg["GOBBY_SESSION_ID"] == "sess-1"

    @pytest.mark.asyncio
    async def test_unset_in_shell_command(self) -> None:
        """Shell command is prefixed with unset VIRTUAL_ENV."""
        spawner = TmuxSpawner()
        with patch.object(
            spawner._session_manager, "create_session", new_callable=AsyncMock
        ) as mock_create:
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=123)
            await spawner._async_spawn(
                command=["claude", "--session-id=xxx"],
                cwd="/tmp",
            )

            mock_create.assert_called_once()
            cmd_arg = (
                mock_create.call_args[1].get("command") or mock_create.call_args[0][1]
                if len(mock_create.call_args[0]) > 1
                else mock_create.call_args[1].get("command")
            )
            assert cmd_arg.startswith("unset VIRTUAL_ENV VIRTUAL_ENV_PROMPT;")

    @pytest.mark.asyncio
    async def test_spawn_returns_success(self) -> None:
        """Successful spawn returns SpawnResult with success=True."""
        spawner = TmuxSpawner()
        with patch.object(
            spawner._session_manager, "create_session", new_callable=AsyncMock
        ) as mock_create:
            mock_create.return_value = TmuxSessionInfo(name="test-session", pane_pid=456)
            result = await spawner._async_spawn(
                command=["echo", "test"],
                cwd="/tmp",
            )

            assert result.success is True
            assert result.pid == 456
            assert result.tmux_session_name == "test-session"

    @pytest.mark.asyncio
    async def test_spawn_failure_returns_error(self) -> None:
        """Failed spawn returns SpawnResult with success=False."""
        spawner = TmuxSpawner()
        with patch.object(
            spawner._session_manager, "create_session", new_callable=AsyncMock
        ) as mock_create:
            mock_create.side_effect = RuntimeError("tmux not found")
            result = await spawner._async_spawn(
                command=["echo", "test"],
                cwd="/tmp",
            )

            assert result.success is False
            assert "tmux not found" in result.error
