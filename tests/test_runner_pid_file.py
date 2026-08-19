"""Regression tests for exclusive daemon PID-file ownership."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

import gobby.runner_lifecycle as runner_lifecycle
from gobby.cli._daemon_services import ServiceStartResult
from gobby.cli.daemon import _do_stop, start
from gobby.cli.runtime import CliRuntime
from gobby.runner import GobbyRunner, main, run_gobby
from gobby.runner_pid_file import (
    FailOpenPidOwnership,
    ProbeState,
    SingletonFsyncError,
    SingletonOpenError,
    SingletonProbe,
    SingletonRecordError,
    SingletonReservationError,
    adopt_inherited_claim,
    cancel_service_reservation,
    claim_pid_file,
    convert_or_acquire_service_claim,
    probe_daemon_lock,
    reserve_service_start,
    service_nonce_path,
)
from tests.runner_helpers import create_base_patches

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


def _daemon_probe(pid: int = 4242) -> SingletonProbe:
    return SingletonProbe(state=ProbeState.DAEMON, pid=pid, role="daemon")


def _cli_runtime() -> CliRunner:
    return CliRunner()


def test_claim_pid_file_refuses_to_overwrite_live_pid(tmp_path: Path) -> None:
    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("4242")

    with patch("gobby.runner_pid_file._pid_is_alive", return_value=True):
        claim = claim_pid_file(pid_file)

    assert claim is None
    assert pid_file.read_text() == "4242"


def test_second_pid_claim_preserves_winners_record(tmp_path: Path) -> None:
    pid_file = tmp_path / "gobby.pid"
    winner = claim_pid_file(pid_file)

    assert winner is not None
    try:
        loser = claim_pid_file(pid_file)
        assert loser is None
        assert pid_file.read_text() == str(os.getpid())
    finally:
        winner.release()


def test_claim_records_owner_pid_in_lock_file(tmp_path: Path) -> None:
    pid_file = tmp_path / "gobby.pid"
    claim = claim_pid_file(pid_file)

    assert claim is not None
    try:
        record = json.loads((tmp_path / "gobby.pid.lock").read_text())
        assert record["pid"] == os.getpid()
        assert record["role"] == "daemon"
    finally:
        claim.release()


def test_probe_daemon_lock_returns_absent_when_free(tmp_path: Path) -> None:
    pid_file = tmp_path / "gobby.pid"

    assert probe_daemon_lock(pid_file).state is ProbeState.ABSENT

    claim = claim_pid_file(pid_file)
    assert claim is not None
    claim.release()
    assert probe_daemon_lock(pid_file).state is ProbeState.ABSENT


def test_probe_daemon_lock_reports_owner_while_held(tmp_path: Path) -> None:
    pid_file = tmp_path / "gobby.pid"
    claim = claim_pid_file(pid_file)

    assert claim is not None
    try:
        probe = probe_daemon_lock(pid_file)
        assert probe.state is ProbeState.DAEMON
        assert probe.pid == os.getpid()
    finally:
        claim.release()


async def test_run_gobby_contention_returns_before_runner_construction(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A losing daemon returns before any mutable subsystem is constructed."""
    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.runner_pid_file.claim_pid_file", return_value=None),
        patch("gobby.runner_pid_file.probe_daemon_lock", return_value=_daemon_probe()),
        patch("gobby.runner.GobbyRunner") as runner_cls,
        caplog.at_level(logging.INFO, logger="gobby.runner"),
    ):
        await run_gobby()

    runner_cls.assert_not_called()
    assert "4242" in caplog.text
    assert "exiting cleanly" in caplog.text


def test_main_exits_zero_on_lock_contention(tmp_path: Path) -> None:
    """A launchd respawn losing the early claim exits 0 (no hot loop)."""
    bootstrap = MagicMock(daemon_port=8765, bind_host="localhost")
    with (
        patch("gobby.config.bootstrap.load_bootstrap", return_value=bootstrap),
        patch("gobby.runner._healthy_daemon_running", return_value=False),
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.runner_pid_file.claim_pid_file", return_value=None),
        patch("gobby.runner_pid_file.probe_daemon_lock", return_value=_daemon_probe()),
        patch("asyncio.run") as mock_asyncio_run,
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 0
    mock_asyncio_run.assert_not_called()


def test_main_passes_early_claim_to_run_gobby(tmp_path: Path) -> None:
    class RecordingClaim:
        def __init__(self) -> None:
            self.release_count = 0

        def release(self) -> None:
            self.release_count += 1

    bootstrap = MagicMock(daemon_port=8765, bind_host="localhost")
    claim = RecordingClaim()
    with (
        patch("gobby.config.bootstrap.load_bootstrap", return_value=bootstrap),
        patch("gobby.runner._healthy_daemon_running", return_value=False),
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.runner_pid_file.claim_pid_file", return_value=claim),
        patch("asyncio.run"),
        patch("gobby.runner.run_gobby") as mock_run_gobby,
    ):
        mock_run_gobby.return_value = None
        main()

    assert mock_run_gobby.call_args.kwargs["ownership_resolution"] is claim
    assert claim.release_count == 1


@pytest.mark.asyncio
async def test_serve_failure_cleans_up_and_exits_zero_when_winner_is_healthy(
    mock_config: MagicMock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    patches = create_base_patches(mock_config=mock_config)
    pid_file = tmp_path / "gobby.pid"
    cleanup_pid_file = MagicMock(side_effect=lambda: pid_file.unlink(missing_ok=True))
    shutdown = AsyncMock()

    async def run_cleanup(*_args: object, **kwargs: object) -> None:
        cleanup = kwargs["cleanup_pid_file"]
        assert callable(cleanup)
        cleanup()

    shutdown.side_effect = run_cleanup
    with ExitStack() as stack:
        [stack.enter_context(item) for item in patches]
        stack.enter_context(patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path))
        stack.enter_context(patch("gobby.runner_maintenance.cleanup_pid_file", cleanup_pid_file))
        stack.enter_context(patch("gobby.runner_maintenance.setup_signal_handlers"))
        stack.enter_context(patch.object(runner_lifecycle, "_init_subsystems", AsyncMock()))
        stack.enter_context(patch.object(runner_lifecycle, "_start_periodic_tasks"))
        stack.enter_context(patch.object(runner_lifecycle, "shutdown_daemon_services", shutdown))
        stack.enter_context(patch("gobby.runner._healthy_daemon_running", return_value=True))
        stack.enter_context(patch("uvicorn.Config"))
        server_class = stack.enter_context(patch("uvicorn.Server"))
        stack.enter_context(
            patch(
                "gobby.servers.uvicorn_shutdown.install_uvicorn_shutdown_filter",
                return_value=MagicMock(),
            )
        )
        stack.enter_context(patch("gobby.servers.uvicorn_shutdown.remove_uvicorn_shutdown_filter"))

        server = SimpleNamespace(started=False, serve=AsyncMock(side_effect=SystemExit(1)))
        server_class.return_value = server
        runner = GobbyRunner()

        with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
            await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

    shutdown.assert_awaited_once()
    cleanup_pid_file.assert_called_once()
    assert runner._shutdown_requested is True
    assert "HTTP server failed before binding (SystemExit(1))" in caplog.text
    assert "requesting daemon shutdown" in caplog.text
    assert not pid_file.exists()


def test_ownership_parameters_have_no_default() -> None:
    """Plan 1.4.19-1.4.21 witness: ownership must be resolved before construction,
    so neither entry point may offer a defaulted ownership parameter."""
    import inspect

    from gobby.runner import GobbyRunner
    from gobby.runner_lifecycle import run_daemon

    run_param = inspect.signature(GobbyRunner.run).parameters["ownership_resolution"]
    daemon_param = inspect.signature(run_daemon).parameters["ownership_resolution"]
    assert run_param.default is inspect.Parameter.empty
    assert daemon_param.default is inspect.Parameter.empty
    assert run_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert daemon_param.kind is inspect.Parameter.KEYWORD_ONLY


class TestRoleBearingClaim:
    def test_default_role_is_daemon(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        claim = claim_pid_file(pid_file)
        assert claim is not None
        try:
            assert claim.role == "daemon"
            probe = probe_daemon_lock(pid_file)
            assert probe.state is ProbeState.DAEMON
            assert probe.role == "daemon"
        finally:
            claim.release()

    def test_maintenance_claim_is_not_daemon(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        claim = claim_pid_file(pid_file, role="maintenance")
        assert claim is not None
        try:
            assert claim.role == "maintenance"
            probe = probe_daemon_lock(pid_file)
            assert probe.state is ProbeState.MAINTENANCE
            assert probe.pid == os.getpid()
            assert probe.is_live_daemon() is False
        finally:
            claim.release()

    def test_daemon_and_maintenance_are_exclusive(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        winner = claim_pid_file(pid_file, role="maintenance")
        assert winner is not None
        try:
            assert claim_pid_file(pid_file, role="daemon") is None
            assert claim_pid_file(pid_file, role="maintenance") is None
        finally:
            winner.release()


class TestServiceReservation:
    def test_reserve_writes_owner_only_nonce_and_live_probe(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="launchd")
        nonce = Path(reservation.nonce_path)
        assert nonce.is_file()
        assert stat.S_IMODE(nonce.stat().st_mode) == 0o600
        probe = probe_daemon_lock(pid_file)
        assert probe.state is ProbeState.LIVE_RESERVATION
        assert probe.reservation is not None
        assert probe.reservation.backend == "launchd"
        assert claim_pid_file(pid_file, role="maintenance") is None
        assert claim_pid_file(pid_file, role="daemon") is None

    def test_convert_matching_nonce_becomes_daemon(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="systemd")
        with patch.dict(
            os.environ,
            {
                "GOBBY_SERVICE_LAUNCH": "1",
                "GOBBY_SERVICE_NONCE": reservation.nonce_path,
            },
            clear=False,
        ):
            claim = convert_or_acquire_service_claim(pid_file)
        assert claim.role == "daemon"
        try:
            probe = probe_daemon_lock(pid_file)
            assert probe.state is ProbeState.DAEMON
            assert not Path(reservation.nonce_path).exists()
        finally:
            claim.release()

    def test_mismatched_nonce_refuses_and_leaves_reservation(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="windows")
        other = tmp_path / "other.nonce"
        other.write_text("not-the-nonce", encoding="utf-8")
        with (
            patch.dict(
                os.environ,
                {"GOBBY_SERVICE_LAUNCH": "1", "GOBBY_SERVICE_NONCE": str(other)},
                clear=False,
            ),
            pytest.raises(SingletonReservationError),
        ):
            convert_or_acquire_service_claim(pid_file)
        probe = probe_daemon_lock(pid_file)
        assert probe.state is ProbeState.LIVE_RESERVATION
        assert Path(reservation.nonce_path).is_file()

    def test_missing_nonce_with_live_reservation_refuses(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="launchd")
        Path(reservation.nonce_path).unlink()
        with (
            patch.dict(
                os.environ,
                {
                    "GOBBY_SERVICE_LAUNCH": "1",
                    "GOBBY_SERVICE_NONCE": reservation.nonce_path,
                },
                clear=False,
            ),
            pytest.raises(SingletonReservationError),
        ):
            convert_or_acquire_service_claim(pid_file)
        assert probe_daemon_lock(pid_file).state is ProbeState.LIVE_RESERVATION

    def test_replay_consumed_nonce_fails(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="launchd")
        env = {
            "GOBBY_SERVICE_LAUNCH": "1",
            "GOBBY_SERVICE_NONCE": reservation.nonce_path,
        }
        with patch.dict(os.environ, env, clear=False):
            claim = convert_or_acquire_service_claim(pid_file)
        claim.release()
        Path(reservation.nonce_path).write_text(reservation.nonce, encoding="utf-8")
        with patch.dict(os.environ, env, clear=False), pytest.raises(SingletonReservationError):
            convert_or_acquire_service_claim(pid_file)
        assert probe_daemon_lock(pid_file).state is ProbeState.ABSENT

    def test_marked_runner_without_reservation_direct_acquires(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        missing = service_nonce_path(pid_file)
        with patch.dict(
            os.environ,
            {"GOBBY_SERVICE_LAUNCH": "1", "GOBBY_SERVICE_NONCE": str(missing)},
            clear=False,
        ):
            claim = convert_or_acquire_service_claim(pid_file)
        assert claim.role == "daemon"
        try:
            assert probe_daemon_lock(pid_file).state is ProbeState.DAEMON
            assert not missing.exists()
        finally:
            claim.release()

    def test_unmarked_runner_does_not_take_service_branch(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="launchd")
        with patch.dict(os.environ, {"GOBBY_SERVICE_NONCE": reservation.nonce_path}, clear=False):
            os.environ.pop("GOBBY_SERVICE_LAUNCH", None)
            assert claim_pid_file(pid_file) is None
        assert probe_daemon_lock(pid_file).state is ProbeState.LIVE_RESERVATION
        assert Path(reservation.nonce_path).is_file()

    def test_expired_reservation_is_cleared_before_admission(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="launchd")
        with patch("gobby.runner_pid_record.time.time", return_value=reservation.issued_at + 31):
            claim = claim_pid_file(pid_file, role="maintenance")
        assert claim is not None
        try:
            assert probe_daemon_lock(pid_file).state is ProbeState.MAINTENANCE
            assert not Path(reservation.nonce_path).exists()
        finally:
            claim.release()

    def test_previous_boot_reservation_is_cleared(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="systemd")
        with patch("gobby.runner_pid_record.current_boot_id", return_value="other-boot"):
            claim = claim_pid_file(pid_file, role="daemon")
        assert claim is not None
        try:
            assert not Path(reservation.nonce_path).exists()
        finally:
            claim.release()

    def test_cleanup_does_not_delete_unrelated_nonce(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="launchd")
        stranger = tmp_path / "stranger.nonce"
        stranger.write_text("keep-me", encoding="utf-8")
        with patch("gobby.runner_pid_record.time.time", return_value=reservation.issued_at + 31):
            claim = claim_pid_file(pid_file)
        assert claim is not None
        claim.release()
        assert stranger.read_text(encoding="utf-8") == "keep-me"
        assert not Path(reservation.nonce_path).exists()

    def test_tampered_nonce_refuses_consume_and_keeps_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="launchd")
        Path(reservation.nonce_path).write_text("tampered", encoding="utf-8")
        with (
            patch.dict(
                os.environ,
                {
                    "GOBBY_SERVICE_LAUNCH": "1",
                    "GOBBY_SERVICE_NONCE": reservation.nonce_path,
                },
                clear=False,
            ),
            pytest.raises(SingletonReservationError),
        ):
            convert_or_acquire_service_claim(pid_file)
        assert Path(reservation.nonce_path).read_text(encoding="utf-8") == "tampered"
        assert probe_daemon_lock(pid_file).state is ProbeState.LIVE_RESERVATION

    def test_fresh_nonce_succeeds_after_parent_crash_cleanup(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        first = reserve_service_start(pid_file, backend="launchd")
        with patch("gobby.runner_pid_record.time.time", return_value=first.issued_at + 31):
            second = reserve_service_start(pid_file, backend="launchd")
        assert second.nonce != first.nonce
        assert Path(second.nonce_path).is_file()
        assert probe_daemon_lock(pid_file).state is ProbeState.LIVE_RESERVATION

    def test_second_reserve_refuses_while_live(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reserve_service_start(pid_file, backend="launchd")
        with pytest.raises(SingletonReservationError):
            reserve_service_start(pid_file, backend="launchd")

    def test_second_reserve_succeeds_when_issuer_pid_is_dead(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        with patch("gobby.runner_pid_file.os.getpid", return_value=999_999):
            first = reserve_service_start(pid_file, backend="launchd")
        with patch("gobby.runner_pid_file._pid_is_alive", return_value=False):
            second = reserve_service_start(pid_file, backend="launchd")
        assert second.nonce != first.nonce
        assert Path(second.nonce_path).is_file()

    def test_probe_reports_stale_reservation_without_clearing(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="windows")
        with patch("gobby.runner_pid_record.time.time", return_value=reservation.issued_at + 31):
            probe = probe_daemon_lock(pid_file)
        assert probe.state is ProbeState.STALE_RESERVATION
        assert Path(reservation.nonce_path).is_file()


class TestTransitioningAndFailures:
    def test_torn_record_is_transitioning(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        lock_path = pid_file.with_name(f"{pid_file.name}.lock")
        lock_path.write_text("{not-json", encoding="utf-8")
        probe = probe_daemon_lock(pid_file)
        assert probe.state is ProbeState.TRANSITIONING

    def test_probe_reports_transitioning_while_lock_held_before_publish(
        self, tmp_path: Path
    ) -> None:
        from gobby.runner_pid_file import _lock_file, _unlock_file
        from gobby.runner_pid_record import write_transitioning_record

        pid_file = tmp_path / "gobby.pid"
        lock_path = pid_file.with_name(f"{pid_file.name}.lock")
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        started = threading.Event()
        release = threading.Event()

        def holder() -> None:
            _lock_file(lock_fd)
            write_transitioning_record(lock_fd, generation=1)
            started.set()
            release.wait(timeout=5)
            _unlock_file(lock_fd)

        thread = threading.Thread(target=holder)
        thread.start()
        assert started.wait(timeout=5)
        try:
            assert probe_daemon_lock(pid_file).state is ProbeState.TRANSITIONING
        finally:
            release.set()
            thread.join(timeout=5)
            os.close(lock_fd)

    def test_open_failure_raises_typed_error(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        with (
            patch("gobby.runner_pid_file.os.open", side_effect=OSError("denied")),
            pytest.raises(SingletonOpenError),
        ):
            claim_pid_file(pid_file)

    def test_fsync_failure_raises_and_releases(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        with (
            patch("gobby.runner_pid_record.os.fsync", side_effect=OSError("disk")),
            pytest.raises((SingletonFsyncError, SingletonRecordError)),
        ):
            claim_pid_file(pid_file)
        assert probe_daemon_lock(pid_file).state is ProbeState.ABSENT

    async def test_run_gobby_does_not_convert_lock_failure_to_fail_open(
        self, tmp_path: Path
    ) -> None:
        with (
            patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
            patch(
                "gobby.runner_pid_file.claim_pid_file",
                side_effect=SingletonOpenError("denied"),
            ),
            patch("gobby.runner.GobbyRunner") as runner_cls,
            pytest.raises(SingletonOpenError),
        ):
            await run_gobby()
        runner_cls.assert_not_called()


class TestInheritedClaim:
    def test_adopt_inherited_fd_does_not_open_second_claim(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        parent = claim_pid_file(pid_file)
        assert parent is not None
        env = parent.inherit_environment()
        adopted = adopt_inherited_claim(pid_file, env=env)
        assert adopted is not None
        try:
            assert claim_pid_file(pid_file) is None
            assert probe_daemon_lock(pid_file).state is ProbeState.DAEMON
        finally:
            adopted.release()


class TestStartStopBarriers:
    def test_start_refuses_maintenance_before_services(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        claim = claim_pid_file(pid_file, role="maintenance")
        assert claim is not None
        services = MagicMock()
        runtime = MagicMock()
        try:
            with (
                patch("gobby.cli.daemon.get_gobby_home", return_value=tmp_path),
                patch("gobby.cli.daemon._start_dependency_errors", return_value=[]),
                patch("gobby.cli.daemon._services_start", services),
                patch("gobby.cli.runtime.get_cli_runtime", runtime),
                patch("gobby.cli.daemon.get_service_status", return_value={"installed": False}),
            ):
                result = _cli_runtime().invoke(start, [])
        finally:
            claim.release()
        assert result.exit_code == 1
        assert "maintenance" in result.output.lower()
        services.assert_not_called()
        runtime.assert_not_called()

    def test_start_refuses_live_reservation_before_services(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reserve_service_start(pid_file, backend="launchd")
        services = MagicMock()
        with (
            patch("gobby.cli.daemon.get_gobby_home", return_value=tmp_path),
            patch("gobby.cli.daemon._start_dependency_errors", return_value=[]),
            patch("gobby.cli.daemon._services_start", services),
            patch("gobby.cli.runtime.get_cli_runtime") as runtime,
        ):
            result = _cli_runtime().invoke(start, [])
        assert result.exit_code == 1
        assert "reservation" in result.output.lower()
        services.assert_not_called()
        runtime.assert_not_called()

    def test_direct_start_inherits_lock_fd(self, tmp_path: Path) -> None:
        services = MagicMock(return_value=ServiceStartResult("skipped", "no compose"))
        process = MagicMock(pid=4242, poll=MagicMock(return_value=None))
        config = MagicMock()
        config.daemon_port = 60888
        config.websocket.port = 60889
        config.bind_host = "127.0.0.1"
        config.logging = MagicMock()
        config.ui.enabled = False
        config.agent_sandbox.enabled = False
        runtime = CliRuntime(config_file=None, config=config)
        with (
            patch("gobby.cli.daemon.get_gobby_home", return_value=tmp_path),
            patch("gobby.cli.daemon._start_dependency_errors", return_value=[]),
            patch("gobby.cli.daemon._services_start", services),
            patch("gobby.cli.runtime.get_cli_runtime", return_value=runtime),
            patch("gobby.cli.daemon.get_service_status", return_value={"installed": False}),
            patch("gobby.cli.daemon.init_local_storage", return_value=MagicMock()),
            patch("gobby.cli.daemon.is_port_available", return_value=True),
            patch("gobby.cli.daemon.subprocess.Popen", return_value=process) as popen,
            patch("gobby.cli.daemon._wait_for_daemon_health", return_value=0.1),
            patch("gobby.cli.daemon._poll_startup_progress", return_value=True),
            patch("gobby.cli.daemon._reconcile_ui_exposure"),
            patch("gobby.cli.daemon.has_auth_env", return_value=True),
            patch("gobby.cli.daemon.time.sleep"),
        ):
            (tmp_path / "logs").mkdir()
            result = _cli_runtime().invoke(start, [])
        assert result.exit_code == 0
        kwargs = popen.call_args.kwargs
        assert "GOBBY_SINGLETON_LOCK_FD" in kwargs["env"]
        assert kwargs.get("pass_fds")

    def test_do_stop_refuses_maintenance_before_runtime(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        claim = claim_pid_file(pid_file, role="maintenance")
        assert claim is not None
        ctx = MagicMock()
        try:
            with (
                patch("gobby.cli.daemon.get_gobby_home", return_value=tmp_path),
                patch("gobby.cli.runtime.get_cli_runtime") as runtime,
                patch("gobby.cli.daemon.get_service_status") as svc,
                patch("gobby.cli.daemon.stop_daemon_util") as stop_util,
            ):
                ok = _do_stop(ctx, docker_flag=False)
        finally:
            claim.release()
        assert ok is False
        runtime.assert_not_called()
        svc.assert_not_called()
        stop_util.assert_not_called()

    def test_do_stop_cancels_live_reservation(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reservation = reserve_service_start(pid_file, backend="launchd")
        ctx = MagicMock()
        with (
            patch("gobby.cli.daemon.get_gobby_home", return_value=tmp_path),
            patch("gobby.cli.runtime.get_cli_runtime") as runtime,
            patch("gobby.cli.daemon.get_service_status") as svc,
            patch("gobby.cli.daemon.stop_daemon_util") as stop_util,
        ):
            ok = _do_stop(ctx, docker_flag=False)
        assert ok is True
        runtime.assert_not_called()
        svc.assert_not_called()
        stop_util.assert_not_called()
        assert probe_daemon_lock(pid_file).state is ProbeState.ABSENT
        assert not Path(reservation.nonce_path).exists()

    def test_cancel_reservation_is_idempotent_after_clear(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        reserve_service_start(pid_file, backend="systemd")
        first = cancel_service_reservation(pid_file)
        second = cancel_service_reservation(pid_file)
        assert first.state is ProbeState.ABSENT
        assert second.state is ProbeState.ABSENT


class TestRunGobbyMaintenanceContention:
    async def test_run_gobby_treats_maintenance_as_contention(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        probe = SingletonProbe(state=ProbeState.MAINTENANCE, pid=99, role="maintenance")
        with (
            patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
            patch("gobby.runner_pid_file.claim_pid_file", return_value=None),
            patch("gobby.runner_pid_file.probe_daemon_lock", return_value=probe),
            patch("gobby.runner.GobbyRunner") as runner_cls,
            caplog.at_level(logging.INFO, logger="gobby.runner"),
        ):
            await run_gobby()
        runner_cls.assert_not_called()
        assert "maintenance" in caplog.text.lower()
        assert "live daemon" not in caplog.text.lower()
