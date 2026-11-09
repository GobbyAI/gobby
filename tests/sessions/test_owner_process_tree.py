"""A background call's recorded jobs are stopped on a graceful owner end, and nothing else (#25793)."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psutil
import pytest

from gobby.sessions import owner_process_tree
from gobby.sessions.owner_process_tree import (
    JobTree,
    capture_job_tree,
    job_tree_of,
    stop_recorded_job_trees,
)
from tests.fixtures.owner_process import (
    live_processes,
    start_owner,
    start_owner_with_orphaned_group,
    start_owner_with_two_groups,
    start_owner_with_writer_and_sibling,
    start_owner_without_a_job_group,
    start_unrelated_process,
    stop_owner,
    wait_for,
)

pytestmark = pytest.mark.unit


def _youngest(processes: list[psutil.Process]) -> psutil.Process:
    """The job, which was started after the shell that leads its group."""
    return max(processes, key=lambda process: process.create_time())


def _oldest(processes: list[psutil.Process]) -> psutil.Process:
    return min(processes, key=lambda process: process.create_time())


def _recorded(context: dict[str, Any], output: Path) -> JobTree:
    tree = capture_job_tree(context, str(output))
    assert tree is not None
    return tree


def _group_holding(owner_pid: int, output: Path) -> int:
    """The group of the owner's job that holds ``output`` open."""
    for job in psutil.Process(owner_pid).children(recursive=True):
        if any(item.path == str(output) for item in job.open_files()):
            return os.getpgid(job.pid)
    raise AssertionError("no job of the owner holds the output file")


def _exited(pid: int) -> bool:
    """Whether the process has exited; an unreaped exit is a zombie, and it counts as exited."""
    try:
        return bool(psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return True


class _KernelPid:
    """Stands in for the pid 0 entry that ``process_iter`` yields on macOS."""

    pid = 0

    def __init__(self) -> None:
        self.signalled: list[str] = []

    def terminate(self) -> None:
        self.signalled.append("terminate")

    def kill(self) -> None:
        self.signalled.append("kill")

    def is_running(self) -> bool:
        return True

    def status(self) -> str:
        return str(psutil.STATUS_RUNNING)


@pytest.fixture
def job_output(tmp_path: Path) -> Path:
    """The file the owner's job writes, which a backgrounded call's receipt names."""
    return tmp_path / "job.output"


@pytest.fixture
def owner(job_output: Path) -> Iterator[tuple[subprocess.Popen[bytes], dict[str, Any]]]:
    process, context = start_owner(job_output)
    try:
        yield process, context
    finally:
        stop_owner(process)


def test_capture_records_the_group_its_shell_leads(
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
    job_output: Path,
) -> None:
    process, context = owner
    shell = _oldest(psutil.Process(process.pid).children(recursive=True))

    tree = _recorded(context, job_output)

    assert shell.pid in {pid for pid, _ in tree.groups}


@pytest.mark.parametrize("context", [{}, {"parent_pid": "not-a-pid"}], ids=["none", "invalid"])
def test_capture_names_nothing_for_an_unverifiable_owner(context: dict[str, Any]) -> None:
    assert capture_job_tree(context, "job.output") is None


def test_capture_names_nothing_for_a_reused_owner_pid(
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
    job_output: Path,
) -> None:
    _, context = owner
    stale = {**context, "parent_create_time": context["parent_create_time"] + 1000.0}

    assert capture_job_tree(stale, str(job_output)) is None


def test_capture_names_nothing_when_no_job_leads_a_group(tmp_path: Path) -> None:
    output = tmp_path / "job.output"
    process, context = start_owner_without_a_job_group(output)
    try:
        assert capture_job_tree(context, str(output)) is None
    finally:
        stop_owner(process)


def test_capture_names_nothing_for_a_call_whose_output_is_unnamed(
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
) -> None:
    _, context = owner

    assert capture_job_tree(context, None) is None


def test_a_writer_sharing_the_owners_group_is_not_recorded_beside_a_sibling(
    tmp_path: Path,
) -> None:
    # The writer is the call's job and shares the owner's group. The sibling leads a group of its
    # own but holds nothing: it is another job, so no tree is recorded for the writer's call.
    output = tmp_path / "job.output"
    process, context = start_owner_with_writer_and_sibling(output)
    jobs = psutil.Process(process.pid).children(recursive=True)
    try:
        assert capture_job_tree(context, str(output)) is None
        assert stop_recorded_job_trees(context, {}) == frozenset()
        assert len(live_processes(jobs)) == 2
    finally:
        stop_owner(process)


def test_capture_records_only_the_group_whose_job_holds_the_output(tmp_path: Path) -> None:
    first = tmp_path / "first.output"
    second = tmp_path / "second.output"
    process, context = start_owner_with_two_groups(first, second)
    try:
        tree = _recorded(context, first)

        assert {pid for pid, _ in tree.groups} == {_group_holding(process.pid, first)}
        assert _group_holding(process.pid, second) not in {pid for pid, _ in tree.groups}
    finally:
        stop_owner(process)


def test_a_recorded_tree_round_trips_and_a_malformed_one_is_refused() -> None:
    tree = JobTree(groups=((4242, 1_700_000_000.5),), sessions=())

    assert job_tree_of(tree.record()) == tree
    assert job_tree_of(None) is None
    assert job_tree_of({"groups": {}, "sessions": {}}) is None
    assert job_tree_of({"groups": {"0": 1.0}, "sessions": {}}) is None
    assert job_tree_of({"groups": {"not-a-pid": 1.0}, "sessions": {}}) is None
    assert job_tree_of({"groups": {"4242": "late"}, "sessions": {}}) is None


def test_stops_the_recorded_jobs_and_passes_their_call(
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
    job_output: Path,
) -> None:
    process, context = owner
    jobs = psutil.Process(process.pid).children(recursive=True)
    tree = _recorded(context, job_output)

    assert stop_recorded_job_trees(context, {"call": tree}) == frozenset({"call"})

    assert live_processes(jobs) == []
    assert process.poll() is None, "the owner itself is left to exit on its own"


def test_a_job_orphaned_after_its_shell_exited_is_stopped_and_its_call_passes(
    tmp_path: Path,
) -> None:
    go_file = tmp_path / "go"
    pid_file = tmp_path / "grandchild.pid"
    output = tmp_path / "job.output"
    process, context = start_owner_with_orphaned_group(go_file, pid_file, output)
    grandchild = psutil.Process(int(pid_file.read_text(encoding="utf-8")))
    try:
        tree = _recorded(context, output)
        shell_pid = psutil.Process(process.pid).children()[0].pid
        go_file.write_text("", encoding="utf-8")
        wait_for(lambda: _exited(shell_pid))

        # The shell is gone and the grandchild is no longer its descendant, but it keeps the
        # shell's group, which is the only link left to it.
        assert grandchild.pid not in {
            job.pid for job in psutil.Process(process.pid).children(recursive=True)
        }
        assert os.getpgid(grandchild.pid) == shell_pid

        assert stop_recorded_job_trees(context, {"call": tree}) == frozenset({"call"})

        assert live_processes([grandchild]) == []
    finally:
        if grandchild.is_running():
            grandchild.kill()
        stop_owner(process)


def test_a_job_that_survives_the_kill_does_not_pass(
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
    job_output: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, context = owner
    survivor = _youngest(psutil.Process(process.pid).children(recursive=True)).pid
    tree = _recorded(context, job_output)
    real_kill = psutil.Process.kill
    real_terminate = psutil.Process.terminate

    def kill(self: psutil.Process) -> None:
        if self.pid != survivor:
            real_kill(self)

    def terminate(self: psutil.Process) -> None:
        if self.pid != survivor:
            real_terminate(self)

    monkeypatch.setattr(psutil.Process, "kill", kill)
    monkeypatch.setattr(psutil.Process, "terminate", terminate)
    monkeypatch.setattr(owner_process_tree, "_GRACE_SECONDS", 0.2)

    assert stop_recorded_job_trees(context, {"call": tree}) == frozenset()

    assert psutil.Process(survivor).is_running()
    os.kill(survivor, signal.SIGKILL)


def test_a_job_the_kernel_refuses_to_signal_does_not_stop_the_sweep(
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
    job_output: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, context = owner
    jobs = psutil.Process(process.pid).children(recursive=True)
    tree = _recorded(context, job_output)
    refused = _oldest(jobs).pid
    stopped = _youngest(jobs)
    real_kill = psutil.Process.kill
    real_terminate = psutil.Process.terminate

    def kill(self: psutil.Process) -> None:
        if self.pid == refused:
            raise psutil.AccessDenied(refused)
        real_kill(self)

    def terminate(self: psutil.Process) -> None:
        if self.pid == refused:
            raise psutil.AccessDenied(refused)
        real_terminate(self)

    monkeypatch.setattr(psutil.Process, "kill", kill)
    monkeypatch.setattr(psutil.Process, "terminate", terminate)
    monkeypatch.setattr(owner_process_tree, "_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(owner_process_tree, "_KILL_WAIT_SECONDS", 0.2)

    try:
        # The refused job stays live, so its call stays foreign; the other job is still stopped.
        assert stop_recorded_job_trees(context, {"call": tree}) == frozenset()
        assert live_processes([stopped]) == []
        assert live_processes([psutil.Process(refused)]) != []
    finally:
        monkeypatch.undo()
        os.kill(refused, signal.SIGKILL)


def test_a_member_whose_group_cannot_be_read_does_not_pass(
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
    job_output: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, context = owner
    jobs = psutil.Process(process.pid).children(recursive=True)
    tree = _recorded(context, job_output)
    unreadable = _youngest(jobs).pid
    real_getpgid = os.getpgid

    def getpgid(pid: int) -> int:
        if pid == unreadable:
            raise PermissionError(pid)
        return real_getpgid(pid)

    monkeypatch.setattr(os, "getpgid", getpgid)

    assert stop_recorded_job_trees(context, {"call": tree}) == frozenset()

    assert live_processes(jobs) != []


def test_a_recorded_leader_now_naming_another_process_is_not_signalled() -> None:
    unrelated = start_unrelated_process(own_session=True)
    try:
        # The recorded leader's pid names another process, so the recorded group is gone. The
        # call's recorded jobs are empty, and the unrelated process is neither member nor target.
        forged = JobTree(groups=((unrelated.pid, 1.0),), sessions=())

        assert stop_recorded_job_trees(None, {"call": forged}) == frozenset({"call"})

        assert live_processes([psutil.Process(unrelated.pid)]) != []
    finally:
        unrelated.kill()
        unrelated.wait(timeout=10)


def test_a_member_that_dies_unreaped_does_not_keep_its_call_from_passing(
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
    job_output: Path,
) -> None:
    # The owner never reaps its shell or the job, so each stays a zombie once it dies.
    process, context = owner
    jobs = psutil.Process(process.pid).children(recursive=True)
    tree = _recorded(context, job_output)

    assert stop_recorded_job_trees(context, {"call": tree}) == frozenset({"call"})

    assert any(job.status() == psutil.STATUS_ZOMBIE for job in jobs if job.is_running())


def test_the_kernel_pid_zero_is_never_signalled(
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
    job_output: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, context = owner
    tree = _recorded(context, job_output)
    kernel = _KernelPid()
    real_iter = psutil.process_iter
    real_getpgid = os.getpgid
    job_group = tree.groups[0][0]

    def process_iter(*args: Any, **kwargs: Any) -> list[Any]:
        return [kernel, *real_iter(*args, **kwargs)]

    def getpgid(pid: int) -> int:
        # The kernel's pid 0 reports the caller's group, which here is the recorded job group.
        return job_group if pid == 0 else real_getpgid(pid)

    monkeypatch.setattr(psutil, "process_iter", process_iter)
    monkeypatch.setattr(os, "getpgid", getpgid)

    assert stop_recorded_job_trees(context, {"call": tree}) == frozenset({"call"})

    assert kernel.signalled == []


def test_a_process_outside_the_recorded_groups_is_left_alone(
    owner: tuple[subprocess.Popen[bytes], dict[str, Any]],
    job_output: Path,
) -> None:
    _, context = owner
    tree = _recorded(context, job_output)
    own_group = start_unrelated_process(own_session=True)
    shared_group = start_unrelated_process(own_session=False)
    try:
        assert stop_recorded_job_trees(context, {"call": tree}) == frozenset({"call"})

        assert (
            len(live_processes([psutil.Process(own_group.pid), psutil.Process(shared_group.pid)]))
            == 2
        )
    finally:
        own_group.kill()
        shared_group.kill()
        own_group.wait(timeout=10)
        shared_group.wait(timeout=10)
