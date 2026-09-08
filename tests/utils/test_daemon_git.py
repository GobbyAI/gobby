from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from gobby.utils.daemon_git import DaemonGitService, GitFailed, GitOk, GitTimeout


def _write_fake_git(tmp_path: Path) -> None:
    executable = tmp_path / "git"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, subprocess, sys, time\n"
        "count_path = os.environ.get('GIT_TEST_COUNT')\n"
        "if count_path:\n"
        "    with open(count_path, 'a', encoding='utf-8') as stream:\n"
        "        stream.write('x')\n"
        "pid_path = os.environ.get('GIT_TEST_PIDS')\n"
        "if pid_path:\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "    pathlib.Path(pid_path).write_text(f'{os.getpid()} {child.pid}')\n"
        "time.sleep(float(os.environ.get('GIT_TEST_DELAY', '0')))\n"
        "print('\\0'.join(sys.argv[1:]), end='')\n"
        "if os.environ.get('GIT_TEST_FAIL'):\n"
        "    print('failed', file=sys.stderr)\n"
        "    raise SystemExit(7)\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)


def _git_env(tmp_path: Path, **values: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env.update(values)
    return env


async def _wait_for_file(path: Path) -> None:
    async with asyncio.timeout(2):
        while not path.exists():
            await asyncio.sleep(0.01)


async def _assert_process_group_gone(process_group: int) -> None:
    async with asyncio.timeout(2):
        while True:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_run_returns_typed_success_and_failure(tmp_path: Path) -> None:
    _write_fake_git(tmp_path)
    service = DaemonGitService()

    success = await service.run(["rev-parse", "HEAD"], cwd=tmp_path, env=_git_env(tmp_path))
    failure = await service.run(
        ["status"],
        cwd=tmp_path,
        env=_git_env(tmp_path, GIT_TEST_FAIL="1"),
    )

    assert isinstance(success, GitOk)
    assert success.stdout == "rev-parse\0HEAD"
    assert isinstance(failure, GitFailed)
    assert failure.returncode == 7
    assert failure.stderr.strip() == "failed"


@pytest.mark.asyncio
async def test_status_coalesces_only_exact_requests(tmp_path: Path) -> None:
    _write_fake_git(tmp_path)
    count = tmp_path / "count"
    service = DaemonGitService()
    env = _git_env(
        tmp_path,
        GIT_TEST_COUNT=str(count),
        GIT_TEST_DELAY="0.05",
    )

    first, second = await asyncio.gather(
        service.status(tmp_path, ["b.py", "a[1].py"], env=env),
        service.status(tmp_path, ["a[1].py", "b.py"], env=env),
    )

    assert isinstance(first, GitOk)
    assert first == second
    assert count.read_text(encoding="utf-8") == "x"
    assert first.stdout.split("\0") == [
        "--literal-pathspecs",
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        "a[1].py",
        "b.py",
    ]

    count.unlink()
    await asyncio.gather(
        service.status(tmp_path, ["a.py"], env=env),
        service.status(tmp_path, ["b.py"], env=env),
    )
    assert count.read_text(encoding="utf-8") == "xx"


@pytest.mark.asyncio
async def test_identical_generic_commands_do_not_coalesce(tmp_path: Path) -> None:
    _write_fake_git(tmp_path)
    count = tmp_path / "count"
    service = DaemonGitService()
    env = _git_env(tmp_path, GIT_TEST_COUNT=str(count), GIT_TEST_DELAY="0.05")

    await asyncio.gather(
        service.run(["update-ref", "refs/test/a", "HEAD"], cwd=tmp_path, env=env),
        service.run(["update-ref", "refs/test/a", "HEAD"], cwd=tmp_path, env=env),
    )

    assert count.read_text(encoding="utf-8") == "xx"


@pytest.mark.asyncio
async def test_disjoint_reads_overlap() -> None:
    both_started = asyncio.Event()
    release = asyncio.Event()
    started: set[tuple[str, ...]] = set()

    class ProbeService(DaemonGitService):
        async def _execute(
            self,
            argv: tuple[str, ...],
            *,
            cwd: str,
            timeout: float,
            env: dict[str, str] | None,
        ) -> GitOk:
            del cwd, timeout, env
            started.add(argv)
            if len(started) == 2:
                both_started.set()
            await release.wait()
            return GitOk("ok", argv, "", "")

    service = ProbeService()
    requests = asyncio.gather(
        service.run(["rev-parse", "HEAD"], cwd="."),
        service.run(["show", "HEAD:file.py"], cwd="."),
    )

    await asyncio.wait_for(both_started.wait(), timeout=0.2)
    release.set()
    await requests


@pytest.mark.asyncio
async def test_cancelling_one_coalesced_waiter_preserves_the_other(tmp_path: Path) -> None:
    _write_fake_git(tmp_path)
    count = tmp_path / "count"
    service = DaemonGitService()
    env = _git_env(tmp_path, GIT_TEST_COUNT=str(count), GIT_TEST_DELAY="0.1")
    first = asyncio.create_task(service.status(tmp_path, ["a.py"], env=env))
    second = asyncio.create_task(service.status(tmp_path, ["a.py"], env=env))

    await asyncio.sleep(0.02)
    first.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first
    assert isinstance(await second, GitOk)
    assert count.read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_timeout_kills_process_group_and_reaps_leader(tmp_path: Path) -> None:
    _write_fake_git(tmp_path)
    pids = tmp_path / "pids"
    service = DaemonGitService()

    result = await service.run(
        ["status"],
        cwd=tmp_path,
        timeout=0.5,
        env=_git_env(tmp_path, GIT_TEST_DELAY="30", GIT_TEST_PIDS=str(pids)),
    )

    assert isinstance(result, GitTimeout)
    leader, _child = map(int, pids.read_text(encoding="utf-8").split())
    await _assert_process_group_gone(leader)


@pytest.mark.asyncio
async def test_cancellation_kills_process_group_and_reaps_leader(tmp_path: Path) -> None:
    _write_fake_git(tmp_path)
    pids = tmp_path / "pids"
    service = DaemonGitService()
    request = asyncio.create_task(
        service.run(
            ["status"],
            cwd=tmp_path,
            timeout=30,
            env=_git_env(tmp_path, GIT_TEST_DELAY="30", GIT_TEST_PIDS=str(pids)),
        )
    )
    await _wait_for_file(pids)

    request.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request
    leader, _child = map(int, pids.read_text(encoding="utf-8").split())
    await _assert_process_group_gone(leader)


@pytest.mark.asyncio
async def test_deadline_includes_slow_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_fake_git(tmp_path)
    real_popen = subprocess.Popen
    created: list[subprocess.Popen[str]] = []

    def slow_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        time.sleep(0.1)
        process = real_popen(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr("gobby.utils.daemon_git.subprocess.Popen", slow_popen)
    service = DaemonGitService()

    result = await service.run(
        ["status"],
        cwd=tmp_path,
        timeout=0.02,
        env=_git_env(tmp_path, GIT_TEST_DELAY="30"),
    )

    assert isinstance(result, GitTimeout)
    assert len(created) == 1
    assert created[0].returncode is not None
    await _assert_process_group_gone(created[0].pid)


@pytest.mark.asyncio
async def test_run_does_not_use_shared_asyncio_worker_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_fake_git(tmp_path)

    async def unexpected_to_thread(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("daemon Git used the shared asyncio worker pool")

    monkeypatch.setattr(asyncio, "to_thread", unexpected_to_thread)

    result = await DaemonGitService().run(
        ["status"],
        cwd=tmp_path,
        env=_git_env(tmp_path),
    )

    assert isinstance(result, GitOk)
