from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.utils import daemon_git
from gobby.utils.daemon_git import (
    DaemonGitService,
    GitFailed,
    GitOk,
    GitTimeout,
    parse_porcelain_v1_z,
)


def test_parse_porcelain_v1_z_preserves_literal_and_rename_paths() -> None:
    entries = parse_porcelain_v1_z(
        " M line\nfeed.py\0R  new name.py\0old name.py\0?? [literal]*.py\0"
    )

    assert [(entry.code, entry.path, entry.original_path) for entry in entries] == [
        (" M", "line\nfeed.py", None),
        ("R ", "new name.py", "old name.py"),
        ("??", "[literal]*.py", None),
    ]


def test_parse_porcelain_v1_z_accepts_empty_status() -> None:
    assert parse_porcelain_v1_z("") == ()


@pytest.mark.parametrize("output", ["\0", "\0\0", " M tracked.py\0\0", " M tracked.py"])
def test_parse_porcelain_v1_z_rejects_empty_or_unterminated_records(output: str) -> None:
    with pytest.raises(ValueError, match="invalid porcelain-v1 status output"):
        parse_porcelain_v1_z(output)


def _write_fake_git(tmp_path: Path) -> None:
    executable = tmp_path / "git"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-C" ]; then cd "$2" || exit 128; shift 2; fi\n'
        'if [ -n "$GIT_TEST_COUNT" ]; then printf x >> "$GIT_TEST_COUNT"; fi\n'
        'if [ -n "$GIT_TEST_PIDS" ]; then\n'
        "  /bin/sleep 30 &\n"
        "  helper_pid=$!\n"
        '  /bin/sleep "${GIT_TEST_DELAY:-0}" &\n'
        "  command_pid=$!\n"
        '  printf \'%s %s %s\' "$$" "$helper_pid" "$command_pid" > "$GIT_TEST_PIDS"\n'
        '  wait "$command_pid"\n'
        "else\n"
        '  /bin/sleep "${GIT_TEST_DELAY:-0}"\n'
        "fi\n"
        'if [ -n "$GIT_TEST_OUTPUT_FILE" ]; then\n'
        '  /bin/cat "$GIT_TEST_OUTPUT_FILE"\n'
        "  exit 0\n"
        "fi\n"
        "first=1\n"
        'for arg in "$@"; do\n'
        "  if [ \"$first\" -eq 0 ]; then printf '\\0'; fi\n"
        "  printf '%s' \"$arg\"\n"
        "  first=0\n"
        "done\n"
        'if [ -n "$GIT_TEST_FAIL" ]; then\n'
        "  printf 'failed\\n' >&2\n"
        "  exit 7\n"
        "fi\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)


def _git_env(tmp_path: Path, **values: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env.update(values)
    return env


class _CompletedStreamProcess:
    """A process double that has already spooled one stdout chunk."""

    pid = 2_147_483_647
    returncode = 0

    def __init__(self, *_args: object, **kwargs: object) -> None:
        stdout = cast(Any, kwargs["stdout_file"])
        stdout.write(b"chunk")

    def wait(self) -> int:
        return self.returncode


async def _wait_for_file(path: Path) -> None:
    async with asyncio.timeout(2):
        while not path.exists():
            await asyncio.sleep(0.01)


async def _assert_processes_gone(*process_ids: int) -> None:
    async with asyncio.timeout(2):
        while True:
            alive: list[int] = []
            for process_id in process_ids:
                try:
                    os.getpgid(process_id)
                except ProcessLookupError:
                    continue
                alive.append(process_id)
            if not alive:
                return
            await asyncio.sleep(0.01)


def _assert_no_new_git_workers(baseline: set[threading.Thread]) -> None:
    leaked = [
        thread
        for thread in threading.enumerate()
        if thread.name == "gobby-daemon-git" and thread not in baseline
    ]
    assert leaked == []


@pytest.mark.asyncio
async def test_run_returns_typed_success_and_failure(tmp_path: Path) -> None:
    _write_fake_git(tmp_path)
    service = DaemonGitService()

    success = await service.run(
        ["rev-parse", "HEAD"], cwd=tmp_path, timeout=2.0, env=_git_env(tmp_path)
    )
    failure = await service.run(
        ["status"],
        cwd=tmp_path,
        timeout=2.0,
        env=_git_env(tmp_path, GIT_TEST_FAIL="1"),
    )

    assert isinstance(success, GitOk)
    assert success.stdout == "rev-parse\0HEAD"
    assert isinstance(failure, GitFailed)
    assert failure.returncode == 7
    assert failure.stderr.strip() == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform_name", "missing_attribute"),
    [
        ("nt", None),
        ("posix", "posix_spawn"),
        ("posix", "POSIX_SPAWN_OPEN"),
        ("posix", "POSIX_SPAWN_DUP2"),
        ("posix", "POSIX_SPAWN_CLOSE"),
    ],
)
async def test_run_uses_a_popen_session_where_posix_spawn_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    missing_attribute: str | None,
) -> None:
    _write_fake_git(tmp_path)
    real_popen = subprocess.Popen
    popen_calls: list[tuple[object, dict[str, Any]]] = []

    def recording_popen(argv: object, **kwargs: Any) -> subprocess.Popen[bytes]:
        popen_calls.append((argv, kwargs))
        return real_popen(cast(Any, argv), **kwargs)

    monkeypatch.setattr("gobby.utils.daemon_git.subprocess.Popen", recording_popen)
    monkeypatch.setattr("gobby.utils.daemon_git.os.name", platform_name)
    if missing_attribute is not None:
        monkeypatch.delattr(f"gobby.utils.daemon_git.os.{missing_attribute}")

    result = await DaemonGitService().run(
        ["rev-parse", "HEAD"], cwd=tmp_path, timeout=2.0, env=_git_env(tmp_path)
    )

    assert isinstance(result, GitOk)
    assert result.stdout == "rev-parse\0HEAD"
    assert [(argv, kwargs["cwd"], kwargs["start_new_session"]) for argv, kwargs in popen_calls] == [
        (("git", "rev-parse", "HEAD"), str(tmp_path), True)
    ]


@pytest.mark.asyncio
async def test_git_commands_start_their_own_process_group_without_forking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_fake_git(tmp_path)
    loop_thread = threading.get_ident()
    spawn_calls: list[tuple[str, tuple[str, ...], dict[str, Any], int]] = []
    real_posix_spawn = os.posix_spawn

    def recording_posix_spawn(
        path: str,
        argv: tuple[str, ...],
        env: dict[str, str],
        **kwargs: Any,
    ) -> int:
        spawn_calls.append((path, argv, kwargs, threading.get_ident()))
        return real_posix_spawn(path, argv, env, **kwargs)

    def forking_popen(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        raise AssertionError("Popen with cwd and a new session forks the daemon")

    monkeypatch.setattr("gobby.utils.daemon_git.os.posix_spawn", recording_posix_spawn)
    monkeypatch.setattr("gobby.utils.daemon_git.subprocess.Popen", forking_popen)
    service = DaemonGitService()
    env = _git_env(tmp_path)
    chunks: list[bytes] = []

    ran = await service.run(["rev-parse", "HEAD"], cwd=tmp_path, timeout=2.0, env=env)
    status = await service.status(tmp_path, ["same.py"], env=env)
    streamed = await service.stream_bytes(
        ["show"], cwd=tmp_path, consume=chunks.append, timeout=2.0, env=env
    )

    assert isinstance(ran, GitOk)
    assert ran.argv == ("git", "rev-parse", "HEAD")
    assert ran.stdout == "rev-parse\0HEAD"
    assert isinstance(status, GitOk)
    assert isinstance(streamed, GitOk)
    assert b"".join(chunks) == b"show"
    git = str(tmp_path / "git")
    assert [(path, argv[:3]) for path, argv, _kwargs, _thread in spawn_calls] == [
        (git, (git, "-C", str(tmp_path)))
    ] * 3
    assert spawn_calls[0][1][3:] == ("rev-parse", "HEAD")
    # Its own session, as Popen's start_new_session: killpg still reaches the group, and a
    # credential prompt cannot stop Git with SIGTTIN from the daemon's terminal.
    assert all(
        kwargs.get("setsid") is True and "setpgroup" not in kwargs
        for _path, _argv, kwargs, _thread in spawn_calls
    )
    assert loop_thread not in {thread for _path, _argv, _kwargs, thread in spawn_calls}


@pytest.mark.asyncio
async def test_run_reports_a_missing_directory_as_git_never_starting(tmp_path: Path) -> None:
    _write_fake_git(tmp_path)
    missing = tmp_path / "missing"

    result = await DaemonGitService().run(
        ["status"], cwd=missing, timeout=2.0, env=_git_env(tmp_path)
    )

    assert isinstance(result, GitFailed)
    assert result.returncode is None
    assert str(missing) in result.stderr


@pytest.mark.asyncio
async def test_stream_bytes_spools_and_preserves_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_git(tmp_path)
    payload = b"prefix\r\nraw-cr\r\x00\xff" + bytes(range(256)) * 1024
    output = tmp_path / "output.bin"
    output.write_bytes(payload)
    real_spawn = daemon_git._spawn_git
    process_streams: list[tuple[object, object]] = []

    def recording_spawn(*args: Any, **kwargs: Any) -> daemon_git._GitProcess:
        process_streams.append((kwargs.get("stdout_file"), kwargs.get("stderr_file")))
        return real_spawn(*args, **kwargs)

    monkeypatch.setattr(daemon_git, "_spawn_git", recording_spawn)
    chunks: list[bytes] = []

    result = await DaemonGitService().stream_bytes(
        ["show"],
        cwd=tmp_path,
        consume=chunks.append,
        timeout=2.0,
        env=_git_env(tmp_path, GIT_TEST_OUTPUT_FILE=str(output)),
    )

    assert isinstance(result, GitOk)
    assert result.stdout == ""
    assert b"".join(chunks) == payload
    assert len(chunks) > 1
    assert max(map(len, chunks)) <= 64 * 1024
    assert len(process_streams) == 1
    assert all(stream not in (None, subprocess.PIPE) for stream in process_streams[0])


@pytest.mark.asyncio
async def test_stream_cancellation_waits_for_active_consumer(tmp_path: Path) -> None:
    _write_fake_git(tmp_path)
    payload = bytes(range(256)) * 1024
    output = tmp_path / "output.bin"
    output.write_bytes(payload)
    entered = threading.Event()
    release = threading.Event()
    chunks: list[bytes] = []

    def consume(chunk: bytes) -> None:
        chunks.append(chunk)
        entered.set()
        assert release.wait(2)

    request = asyncio.create_task(
        DaemonGitService().stream_bytes(
            ["show"],
            cwd=tmp_path,
            consume=consume,
            timeout=10.0,
            env=_git_env(tmp_path, GIT_TEST_OUTPUT_FILE=str(output)),
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)

    request.cancel()
    await asyncio.sleep(0)
    try:
        assert not request.done()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 1)
    assert chunks == [payload[: 64 * 1024]]


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
        service.status(tmp_path, ["b.py", "a[1].py"], timeout=60.0, env=env),
        service.status(tmp_path, ["a[1].py", "b.py"], timeout=60.0, env=env),
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
        service.status(tmp_path, ["a.py"], timeout=60.0, env=env),
        service.status(tmp_path, ["b.py"], timeout=60.0, env=env),
    )
    assert count.read_text(encoding="utf-8") == "xx"


@pytest.mark.asyncio
async def test_identical_generic_commands_do_not_coalesce(tmp_path: Path) -> None:
    _write_fake_git(tmp_path)
    count = tmp_path / "count"
    service = DaemonGitService()
    env = _git_env(tmp_path, GIT_TEST_COUNT=str(count), GIT_TEST_DELAY="0.05")

    await asyncio.gather(
        service.run(["update-ref", "refs/test/a", "HEAD"], cwd=tmp_path, timeout=60.0, env=env),
        service.run(["update-ref", "refs/test/a", "HEAD"], cwd=tmp_path, timeout=60.0, env=env),
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
            input_text: str | None,
        ) -> GitOk:
            del cwd, timeout, env, input_text
            started.add(argv)
            if len(started) == 2:
                both_started.set()
            await release.wait()
            return GitOk("ok", argv, "", "")

    service = ProbeService()
    requests = asyncio.gather(
        service.status(".", ["first.py"]),
        service.status(".", ["second.py"]),
    )

    await asyncio.wait_for(both_started.wait(), timeout=0.2)
    assert len(started) == 2
    release.set()
    assert all(isinstance(result, GitOk) for result in await requests)


@pytest.mark.asyncio
async def test_cancelling_one_coalesced_waiter_preserves_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def execute(argv: tuple[str, ...], **kwargs: object) -> GitOk:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return GitOk("ok", argv, "", "")

    service = DaemonGitService()
    monkeypatch.setattr(service, "_execute", execute)
    first = asyncio.create_task(service.status(tmp_path, ["a.py"]))
    second = asyncio.create_task(service.status(tmp_path, ["a.py"]))

    await asyncio.wait_for(started.wait(), timeout=1)
    first.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert isinstance(await second, GitOk)
    assert calls == 1


@pytest.mark.asyncio
async def test_timeout_kills_process_group_and_reaps_leader(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING", logger="gobby.utils.daemon_git")
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
    process_ids = tuple(map(int, pids.read_text(encoding="utf-8").split()))
    leader = process_ids[0]
    assert "phase=running" in result.stderr
    assert "spawn_seconds=" in result.stderr
    assert "running_seconds=" in result.stderr
    await _assert_processes_gone(*process_ids)

    warnings = [
        record
        for record in caplog.records
        if record.name == "gobby.utils.daemon_git" and record.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(tmp_path) in message
    assert f"pid={leader}" in message
    assert "phase=running" in message
    assert "timeout_seconds=0.500" in message
    assert "spawn_seconds=" in message
    assert "running_seconds=" in message


@pytest.mark.asyncio
async def test_nonstream_timeout_waits_for_worker_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_fake_git(tmp_path)
    real_spawn = daemon_git._spawn_git
    release_cleanup = threading.Event()

    class DelayedCompletionProcess:
        def __init__(self, process: daemon_git._GitProcess) -> None:
            self._process = process

        def __getattr__(self, name: str) -> object:
            return getattr(self._process, name)

        def communicate(self, input_bytes: bytes | None = None) -> tuple[bytes, bytes]:
            output = self._process.communicate(input_bytes)
            assert release_cleanup.wait(1)
            return output

    def delayed_spawn(*args: Any, **kwargs: Any) -> DelayedCompletionProcess:
        return DelayedCompletionProcess(real_spawn(*args, **kwargs))

    monkeypatch.setattr(daemon_git, "_spawn_git", delayed_spawn)
    asyncio.get_running_loop().call_later(0.35, release_cleanup.set)

    started = time.monotonic()
    result = await DaemonGitService().run(
        ["status"],
        cwd=tmp_path,
        timeout=0.02,
        env=_git_env(tmp_path, GIT_TEST_DELAY="30"),
    )
    elapsed = time.monotonic() - started

    assert isinstance(result, GitTimeout)
    assert release_cleanup.is_set()
    assert elapsed >= 0.3
    cleanup_match = re.search(r"cleanup_seconds=([0-9.]+)", result.stderr)
    assert cleanup_match is not None
    assert float(cleanup_match.group(1)) >= 0.04


@pytest.mark.asyncio
async def test_stream_timeout_waits_for_callback_longer_than_cleanup_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon_git, "_spawn_git", _CompletedStreamProcess)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    returned = threading.Event()
    writes: list[tuple[bytes, bool]] = []

    def consume(chunk: bytes) -> None:
        entered.set()
        assert release.wait(2)
        writes.append((chunk, returned.is_set()))
        finished.set()

    request = asyncio.create_task(
        DaemonGitService().stream_bytes(
            ["show"],
            cwd=tmp_path,
            consume=consume,
            timeout=0.05,
            env={},
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    premature_result: GitOk | GitFailed | GitTimeout | None = None
    try:
        try:
            premature_result = await asyncio.wait_for(asyncio.shield(request), 0.35)
        except TimeoutError:
            pass
    finally:
        if premature_result is not None:
            returned.set()
        release.set()

    result = premature_result or await asyncio.wait_for(request, 1)
    returned.set()
    assert await asyncio.to_thread(finished.wait, 1)

    assert premature_result is None
    assert isinstance(result, GitTimeout)
    assert "phase=consuming" in result.stderr
    assert writes == [(b"chunk", False)]


@pytest.mark.asyncio
async def test_stream_cancellation_waits_for_callback_longer_than_cleanup_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon_git, "_spawn_git", _CompletedStreamProcess)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cancellation_returned = threading.Event()
    writes: list[tuple[bytes, bool]] = []

    def consume(chunk: bytes) -> None:
        entered.set()
        assert release.wait(2)
        writes.append((chunk, cancellation_returned.is_set()))
        finished.set()

    request = asyncio.create_task(
        DaemonGitService().stream_bytes(
            ["show"],
            cwd=tmp_path,
            consume=consume,
            timeout=10,
            env={},
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    request.cancel()
    cancelled_early = False
    try:
        try:
            await asyncio.wait_for(asyncio.shield(request), 0.35)
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            cancelled_early = True
            cancellation_returned.set()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await request
    cancellation_returned.set()
    assert await asyncio.to_thread(finished.wait, 1)

    assert not cancelled_early
    assert writes == [(b"chunk", False)]


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
    process_ids = tuple(map(int, pids.read_text(encoding="utf-8").split()))
    await _assert_processes_gone(*process_ids)


@pytest.mark.asyncio
async def test_deadline_includes_slow_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_fake_git(tmp_path)
    real_spawn = daemon_git._spawn_git
    created: list[daemon_git._GitProcess] = []
    release_spawn = threading.Event()
    finished = threading.Event()

    def slow_spawn(*args: Any, **kwargs: Any) -> daemon_git._GitProcess:
        release_spawn.wait(5)
        process = real_spawn(*args, **kwargs)
        created.append(process)
        finished.set()
        return process

    monkeypatch.setattr(daemon_git, "_spawn_git", slow_spawn)
    service = DaemonGitService()
    asyncio.get_running_loop().call_later(0.08, release_spawn.set)

    started = time.monotonic()
    result = await asyncio.wait_for(
        service.run(
            ["status"],
            cwd=tmp_path,
            timeout=0.02,
            env=_git_env(tmp_path, GIT_TEST_DELAY="30"),
        ),
        timeout=0.5,
    )

    assert isinstance(result, GitTimeout)
    assert 0.07 <= time.monotonic() - started < 0.5
    assert "phase=spawning" in result.stderr
    assert finished.is_set()
    assert len(created) == 1
    await _assert_processes_gone(created[0].pid)
    assert created[0].returncode is not None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["argv", "env", "input"])
async def test_invalid_worker_arguments_resolve_completion(tmp_path: Path, invalid: str) -> None:
    result = await asyncio.wait_for(
        DaemonGitService().run(
            cast(list[str], [object()]) if invalid == "argv" else ["hash-object", "--stdin"],
            cwd=tmp_path,
            timeout=10,
            env=cast(dict[str, str], {"INVALID": object()}) if invalid == "env" else None,
            input_text=cast(str, object()) if invalid == "input" else None,
        ),
        timeout=1,
    )
    assert isinstance(result, GitFailed)
    assert result.returncode is None
    assert result.stderr


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_does_not_join_a_cancelled_last_waiter_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def execute(argv: tuple[str, ...], **kwargs: object) -> GitOk:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Future[None]()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise
        return GitOk("ok", argv, "replacement", "")

    service = DaemonGitService()
    monkeypatch.setattr(service, "_execute", execute)
    first = asyncio.create_task(service.status(".", ["a.py"]))
    await asyncio.wait_for(started.wait(), 1)
    first.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    try:
        replacement = await asyncio.wait_for(service.status(".", ["a.py"]), 1)
        assert isinstance(replacement, GitOk)
        assert replacement.stdout == "replacement"
        assert calls == 2
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert not service._status_inflight


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_does_not_coalesce_across_event_loop_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_git(tmp_path)
    real_spawn = daemon_git._spawn_git
    barrier = threading.Barrier(2)
    count = tmp_path / "count"
    env = _git_env(tmp_path, GIT_TEST_COUNT=str(count))

    def overlapping_spawn(*args: Any, **kwargs: Any) -> daemon_git._GitProcess:
        barrier.wait(timeout=2)
        return real_spawn(*args, **kwargs)

    monkeypatch.setattr(daemon_git, "_spawn_git", overlapping_spawn)
    service = DaemonGitService()

    def other_loop() -> GitOk | GitFailed | GitTimeout:
        return asyncio.run(service.status(tmp_path, ["same.py"], env=env))

    first, second = await asyncio.gather(
        service.status(tmp_path, ["same.py"], env=env),
        asyncio.to_thread(other_loop),
    )
    assert isinstance(first, GitOk)
    assert isinstance(second, GitOk)
    assert count.read_text() == "xx"
    assert not service._status_inflight


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disjoint_status_processes_overlap(tmp_path: Path) -> None:
    executable = tmp_path / "git"
    executable.write_text(
        "#!/bin/sh\n"
        'for path in "$@"; do :; done\n'
        'touch "$GIT_TEST_READY/$path"\n'
        'while [ ! -e "$GIT_TEST_READY/a.py" ] || [ ! -e "$GIT_TEST_READY/b.py" ]; do\n'
        "  /bin/sleep 0.01\n"
        "done\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    ready = tmp_path / "ready"
    ready.mkdir()
    service = DaemonGitService()
    env = _git_env(tmp_path, GIT_TEST_READY=str(ready))
    first, second = await asyncio.gather(
        service.status(tmp_path, ["a.py"], timeout=2, env=env),
        service.status(tmp_path, ["b.py"], timeout=2, env=env),
    )
    assert isinstance(first, GitOk)
    assert isinstance(second, GitOk)


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
        timeout=60.0,
        env=_git_env(tmp_path),
    )

    assert isinstance(result, GitOk)
