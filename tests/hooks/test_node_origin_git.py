"""Node-origin git runs on the node, not in the daemon (#24523).

A node-origin delivery sends its git and existence checks through the checkout-read
seam and starts no local git process for them. Local deliveries spawn as before.
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks import _normalization_git, checkout_reads, code_navigation_recovery
from gobby.hooks.checkout_reads import delivery_scope
from gobby.utils import daemon_git, spawn
from gobby.workflows import tdd_paths
from gobby.workflows.tdd_paths import _git_out
from tests.hooks.checkout_read_fakes import NODE_MACHINE, FakeNode

pytestmark = pytest.mark.unit

_TIMEOUT = 5.0
_OTHER_NODE = "machine-c"


@pytest.mark.asyncio
@pytest.mark.parametrize("input_text", [None, "\udcff"])
async def test_node_git_round_trips_non_utf8_bytes(
    node: FakeNode, spawned: list[list[str]], tmp_path: Path, input_text: str | None
) -> None:
    cwd = str(tmp_path)
    node.set_git(NODE_MACHINE, stdout=b"\xff\0")
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), cwd):
        result = await daemon_git.DaemonGitService().run(
            ["status", "--porcelain", "-z"], cwd=cwd, timeout=_TIMEOUT, input_text=input_text
        )
    assert isinstance(result, daemon_git.GitOk)
    assert result.stdout == "\udcff\0"
    assert os.fsencode(result.stdout) == b"\xff\0"
    stdin = node.requests[0][2]["query"]["stdin"]
    assert stdin == (None if input_text is None else base64.b64encode(b"\xff").decode("ascii"))
    assert spawned == []


@pytest.mark.asyncio
async def test_node_git_decodes_stderr_like_local_worker(
    node: FakeNode, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def completed(
        args: list[str], *, cwd: str, timeout: float, input_bytes: bytes | None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 3, stdout=b"", stderr=b"\xff")

    monkeypatch.setattr(checkout_reads, "run_git_async", completed)
    with delivery_scope(NODE_MACHINE, asyncio.get_running_loop(), str(tmp_path)):
        result = await daemon_git.DaemonGitService().run(
            ["log"], cwd=str(tmp_path), timeout=_TIMEOUT
        )
    assert isinstance(result, daemon_git.GitFailed)
    assert result.returncode == 3
    assert result.stderr == "\udcff"


@pytest.mark.asyncio
@pytest.mark.parametrize("node_first", [False, True])
async def test_worktree_identity_cache_is_machine_scoped(
    node: FakeNode, spawned: list[list[str]], tmp_path: Path, node_first: bool
) -> None:
    cwd = str(tmp_path.resolve())
    subprocess.run(["git", "-C", cwd, "init", "-q"], check=True)
    spawned.clear()
    node.set_git(NODE_MACHINE, stdout=b"/node/root\n/node/common\n")
    node.set_git(_OTHER_NODE, stdout=b"/other/root\n/other/common\n")
    machines = (
        [NODE_MACHINE, None, _OTHER_NODE] if node_first else [None, NODE_MACHINE, _OTHER_NODE]
    )
    expected = {
        None: tmp_path.resolve(),
        NODE_MACHINE: Path("/node/root"),
        _OTHER_NODE: Path("/other/root"),
    }
    tdd_paths._git_identity.cache_clear()
    try:
        for machine in machines + machines:
            with delivery_scope(machine, asyncio.get_running_loop(), cwd):
                root = await asyncio.to_thread(
                    tdd_paths.same_repo_worktree_root, tmp_path, tmp_path
                )
            assert root == expected[machine]
        assert [machine for machine, _, _, _ in node.requests] == [NODE_MACHINE, _OTHER_NODE]
        assert all(request[2]["query"]["args"][0] == "rev-parse" for request in node.requests)
        assert len(spawned) == 1
    finally:
        tdd_paths._git_identity.cache_clear()


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    fake = FakeNode()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[list[str]]]:
    """Record every local git process start: the shared spawn helper and daemon_git's spawn."""
    calls: list[list[str]] = []
    run_original: Callable[..., Any] = spawn.run
    git_original: Callable[..., Any] = daemon_git._spawn_git

    def record_run(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        calls.append(list(argv))
        return run_original(argv, *args, **kwargs)

    def record_git(argv: tuple[str, ...], **kwargs: Any) -> Any:
        calls.append(list(argv))
        return git_original(argv, **kwargs)

    monkeypatch.setattr(spawn, "run", record_run)
    monkeypatch.setattr(daemon_git, "_spawn_git", record_git)
    yield calls


@pytest.mark.asyncio
async def test_node_origin_git_runs_on_node(
    node: FakeNode, spawned: list[list[str]], tmp_path: Path
) -> None:
    cwd = str(tmp_path)
    loop = asyncio.get_running_loop()
    service = daemon_git.DaemonGitService()

    node.set_git(NODE_MACHINE, exit_code=0, stdout=b"abc\n")
    with delivery_scope(NODE_MACHINE, loop, cwd):
        ran = await service.run(["log", "-1"], cwd=cwd, timeout=7.5)
        status = await service.status(cwd, ("a.txt",), timeout=7.5)
    assert ran == daemon_git.GitOk("ok", ("git", "log", "-1"), "abc\n", "", 0)
    assert isinstance(status, daemon_git.GitOk)
    sent = [(machine, payload["query"]["kind"], ms) for machine, _, payload, ms in node.requests]
    assert sent == [(NODE_MACHINE, "git", 7500), (NODE_MACHINE, "git", 7500)]

    node.set_git(NODE_MACHINE, exit_code=3, stderr="boom\n")
    with delivery_scope(NODE_MACHINE, loop, cwd):
        failed = await service.run(["log", "-1"], cwd=cwd, timeout=_TIMEOUT)
    assert isinstance(failed, daemon_git.GitFailed)
    assert failed.returncode == 3
    assert failed.stderr == "boom\n"

    node.set_git(NODE_MACHINE, exit_code=None, timed_out=True)
    with delivery_scope(NODE_MACHINE, loop, cwd):
        timed_out = await service.run(["log", "-1"], cwd=cwd, timeout=_TIMEOUT)
    assert isinstance(timed_out, daemon_git.GitTimeout)

    assert spawned == []


@pytest.mark.asyncio
async def test_status_coalescing_is_machine_scoped(
    node: FakeNode, spawned: list[list[str]], tmp_path: Path
) -> None:
    cwd = str(tmp_path)
    loop = asyncio.get_running_loop()
    service = daemon_git.DaemonGitService()
    node.set_git(NODE_MACHINE, exit_code=0, stdout=b"")
    node.set_git(_OTHER_NODE, exit_code=0, stdout=b"")

    async def status_from(machine: str) -> daemon_git.GitResult:
        with delivery_scope(machine, loop, cwd):
            return await service.status(cwd, ("a.txt",), timeout=_TIMEOUT)

    first, second = await asyncio.gather(status_from(NODE_MACHINE), status_from(_OTHER_NODE))
    assert isinstance(first, daemon_git.GitOk)
    assert isinstance(second, daemon_git.GitOk)
    assert [machine for machine, *_ in node.requests] == [NODE_MACHINE, _OTHER_NODE]

    node.requests.clear()
    same = await asyncio.gather(status_from(NODE_MACHINE), status_from(NODE_MACHINE))
    assert all(isinstance(result, daemon_git.GitOk) for result in same)
    assert [machine for machine, *_ in node.requests] == [NODE_MACHINE]
    assert spawned == []


@pytest.mark.asyncio
async def test_unanswered_node_git_fails_with_diagnostic(
    node: FakeNode, spawned: list[list[str]], tmp_path: Path
) -> None:
    cwd = str(tmp_path)
    loop = asyncio.get_running_loop()
    service = daemon_git.DaemonGitService()

    node.fail(NODE_MACHINE, "node_offline")
    with delivery_scope(NODE_MACHINE, loop, cwd):
        offline = await service.run(["log", "-1"], cwd=cwd, timeout=_TIMEOUT)
    assert isinstance(offline, daemon_git.GitFailed)
    assert "checkout_read_unavailable: node_offline" in offline.stderr

    node.recover(NODE_MACHINE)
    node.set_git(NODE_MACHINE, exit_code=0, stdout=b"x" * 16, truncated=True)
    with delivery_scope(NODE_MACHINE, loop, cwd):
        cut = await service.run(["log", "-1"], cwd=cwd, timeout=_TIMEOUT)
    assert isinstance(cut, daemon_git.GitFailed)
    assert "checkout_read_unavailable: truncated" in cut.stderr

    node.requests.clear()
    with delivery_scope(NODE_MACHINE, loop, cwd):
        env_case = await service.run(
            ["log", "-1"], cwd=cwd, timeout=_TIMEOUT, env={"GIT_DIR": "/elsewhere"}
        )
    assert isinstance(env_case, daemon_git.GitFailed)
    assert env_case.stderr == (
        f"checkout_read_unavailable: env_unsupported (machine {NODE_MACHINE})"
    )
    assert node.requests == []
    assert spawned == []


@pytest.mark.asyncio
async def test_sync_git_sites_route_by_origin(
    node: FakeNode, spawned: list[list[str]], tmp_path: Path
) -> None:
    cwd = str(tmp_path)
    loop = asyncio.get_running_loop()
    subprocess.run(["git", "-C", cwd, "init", "-q"], check=True)
    ignored = tmp_path / "ignored.log"
    node.add_file(NODE_MACHINE, "file.txt", b"node")

    node.set_git(NODE_MACHINE, stdout=b"abc\n")
    with delivery_scope(NODE_MACHINE, loop, cwd):
        toplevel = await asyncio.to_thread(_git_out, tmp_path, "rev-parse", "--show-toplevel")
    assert toplevel == "abc"

    node.set_git(NODE_MACHINE, stdout=os.fsencode(str(ignored)) + b"\0")
    with delivery_scope(NODE_MACHINE, loop, cwd):
        ignored_paths = await asyncio.to_thread(
            code_navigation_recovery._git_ignored, tmp_path, [ignored]
        )
    assert ignored_paths == {ignored}

    node.set_git(NODE_MACHINE, stdout=b"")
    with delivery_scope(NODE_MACHINE, loop, cwd):
        is_path = await asyncio.to_thread(
            _normalization_git._positional_checkout_is_path, ("file.txt",), cwd, None
        )
    assert is_path is True
    assert {payload["query"]["kind"] for _, _, payload, _ in node.requests} == {"git", "files"}
    assert spawned == []

    # Local deliveries still spawn git here.
    local = await asyncio.to_thread(_git_out, tmp_path, "rev-parse", "--is-inside-work-tree")
    assert local == "true"
    assert spawned == [["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"]]


def test_positional_checkout_joins_relative_operand_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon_cwd = tmp_path / "daemon"
    checkout = tmp_path / "checkout"
    daemon_cwd.mkdir()
    checkout.mkdir()
    (checkout / "file.txt").write_text("x")
    monkeypatch.chdir(daemon_cwd)

    assert (
        _normalization_git._positional_checkout_is_path(("file.txt",), str(checkout), None) is True
    )


@pytest.mark.asyncio
async def test_node_outside_path_is_unknown_so_the_caller_writes(
    node: FakeNode, spawned: list[list[str]], tmp_path: Path
) -> None:
    cwd = str(tmp_path)
    loop = asyncio.get_running_loop()
    node.add_outside(NODE_MACHINE, "elsewhere.txt")

    with delivery_scope(NODE_MACHINE, loop, cwd):
        is_path = await asyncio.to_thread(
            _normalization_git._positional_checkout_is_path, ("elsewhere.txt",), cwd, None
        )
    # None is fail-closed: the caller treats an unknown checkout as a write, and no ls-files runs.
    assert is_path is None
    assert [payload["query"]["kind"] for _, _, payload, _ in node.requests] == ["files"]
    assert spawned == []


def _caller_thread_state() -> tuple[bool, bool]:
    """Whether the caller is the main thread, and whether an event loop runs on it."""
    thread_is_main = threading.current_thread() is threading.main_thread()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return thread_is_main, False
    return thread_is_main, True


@pytest.mark.asyncio
async def test_local_git_spawn_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A local delivery reaches spawn.run through run_git_async's to_thread worker, never the loop.
    cwd = str(tmp_path)
    subprocess.run(["git", "-C", cwd, "init", "-q"], check=True)
    states: list[tuple[bool, bool]] = []
    spawn_run = spawn.run

    def recording_run(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        states.append(_caller_thread_state())
        return spawn_run(argv, *args, **kwargs)

    monkeypatch.setattr(spawn, "run", recording_run)
    completed = await checkout_reads.run_git_async(
        ["rev-parse", "--is-inside-work-tree"], cwd=cwd, timeout=_TIMEOUT
    )
    assert completed.stdout.strip() == b"true"
    assert states == [(False, False)]
