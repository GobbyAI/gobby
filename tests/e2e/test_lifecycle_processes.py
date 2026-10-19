"""Lifecycle interruptions must drain detached native replacements and backends."""

import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest
from pytest_mock import MockerFixture

from tests.e2e import lifecycle_processes
from tests.e2e.lifecycle_processes import LifecycleProcesses

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("interruption", ["timeout", "pytest-timeout", "sigint"])
def test_interrupted_restart_cleans_detached_replacement(
    tmp_path: Path, mocker: MockerFixture, interruption: str
) -> None:
    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("10")
    processes: dict[int, MagicMock] = {}
    for pid in (10, 11, 20, 30, 31):
        process = mocker.create_autospec(psutil.Process, instance=True)
        process.pid = pid
        process.is_running.return_value = True
        process.children.return_value = []
        processes[pid] = process
    processes[10].children.return_value = [processes[11]]
    processes[30].children.return_value = [processes[31]]
    mocker.patch("tests.e2e.lifecycle_processes.psutil.Process", side_effect=processes.__getitem__)
    child = mocker.MagicMock(spec=subprocess.Popen)
    child.pid = 20
    child.stdout = mocker.MagicMock()
    child.stderr = mocker.MagicMock()
    mocker.patch("tests.e2e.lifecycle_processes.subprocess.Popen", return_value=child)
    clock = [0.0]
    mocker.patch("tests.e2e.lifecycle_processes.time.monotonic", side_effect=lambda: clock[0])
    stopped: list[int] = []

    def stop(pid: int) -> None:
        stopped.append(pid)
        processes[pid].is_running.return_value = False

    mocker.patch.object(lifecycle_processes, "terminate_process_tree", side_effect=stop)
    owner = LifecycleProcesses(pid_file)
    calls = 0
    failure: BaseException
    if interruption == "timeout":
        failure = subprocess.TimeoutExpired("isolated-cli", 1)
    elif interruption == "pytest-timeout":
        failure = pytest.fail.Exception("Timeout (>60.0s) from pytest-timeout.")
    else:
        failure = KeyboardInterrupt()

    def communicate(*, timeout: float) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            # The old owner exited and the replacement detached before CLI EOF.
            processes[10].is_running.return_value = False
            pid_file.write_text("30")
            raise subprocess.TimeoutExpired("isolated-cli", timeout)
        assert {30, 31} <= owner.processes.keys()
        clock[0] = 2.0
        raise failure

    child.communicate.side_effect = communicate
    with pytest.raises(type(failure)) as caught:
        with owner:
            owner.track(10)
            owner.run(["isolated-cli"], env={}, cwd=tmp_path, timeout=1)

    assert caught.value is failure
    assert set(stopped) == {11, 20, 30, 31}
    assert stopped[0] == 20
    child.wait.assert_called_once_with(timeout=1)
    child.stdout.close.assert_called_once_with()
    child.stderr.close.assert_called_once_with()


def test_cleanup_discovers_daemon_even_when_cli_never_returns(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Final discovery covers a replacement published at the interruption seam."""
    pid_file = tmp_path / "gobby.pid"
    owner = LifecycleProcesses(pid_file)
    native = mocker.create_autospec(psutil.Process, instance=True)
    native.pid = 30
    native.is_running.return_value = True
    backend = mocker.create_autospec(psutil.Process, instance=True)
    backend.pid = 31
    backend.is_running.return_value = True
    native.children.return_value = [backend]
    mocker.patch("tests.e2e.lifecycle_processes.psutil.Process", return_value=native)
    terminate = mocker.patch.object(lifecycle_processes, "terminate_process_tree")

    with pytest.raises(KeyboardInterrupt):
        with owner:
            pid_file.write_text("30")
            raise KeyboardInterrupt()

    assert terminate.call_args_list == [mocker.call(30), mocker.call(31)]


def test_success_preserves_exit_status_output_and_closes_cli_pipes(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    pid_file = tmp_path / "gobby.pid"
    process = mocker.create_autospec(psutil.Process, instance=True)
    process.pid = 20
    process.is_running.return_value = True
    process.children.return_value = []
    mocker.patch("tests.e2e.lifecycle_processes.psutil.Process", return_value=process)
    child = mocker.MagicMock(spec=subprocess.Popen)
    child.pid = 20
    child.returncode = 7
    stdout = StringIO()
    stderr = StringIO()
    child.stdout = stdout
    child.stderr = stderr
    child.communicate.return_value = ("startup output", "startup error")
    mocker.patch("tests.e2e.lifecycle_processes.subprocess.Popen", return_value=child)
    terminate = mocker.patch.object(lifecycle_processes, "terminate_process_tree")
    with LifecycleProcesses(pid_file) as owner:
        result = owner.run(["isolated-cli"], env={}, cwd=tmp_path, timeout=1)
    assert (result.returncode, result.stdout, result.stderr) == (
        7,
        "startup output",
        "startup error",
    )
    terminate.assert_called_with(20)
    child.wait.assert_called_once_with(timeout=1)
    assert stdout.closed
    assert stderr.closed


def test_cleanup_failure_does_not_replace_original_interrupt(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    process = mocker.create_autospec(psutil.Process, instance=True)
    process.pid = 20
    process.is_running.return_value = True
    process.children.return_value = []
    mocker.patch("tests.e2e.lifecycle_processes.psutil.Process", return_value=process)
    mocker.patch.object(
        lifecycle_processes, "terminate_process_tree", side_effect=psutil.AccessDenied(20)
    )
    interruption = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught:
        with LifecycleProcesses(tmp_path / "gobby.pid") as owner:
            owner.track(20)
            raise interruption
    assert caught.value is interruption
    assert any("cleanup failed" in note for note in caught.value.__notes__)
