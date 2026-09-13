"""Shared fixtures for the cross-backend runtime contract suite (plan 5.1)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest


def _gterm_socket_dir(cmdline: list[str]) -> Path | None:
    if len(cmdline) < 2 or Path(cmdline[0]).name != "gterm" or cmdline[1] != "host":
        return None
    try:
        socket_index = cmdline.index("--socket-dir") + 1
        return Path(cmdline[socket_index]).resolve()
    except (ValueError, IndexError):
        return None


def _gterm_hosts() -> dict[int, Path]:
    hosts: dict[int, Path] = {}
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            socket_dir = _gterm_socket_dir(process.info.get("cmdline") or [])
            if socket_dir is not None:
                hosts[int(process.info["pid"])] = socket_dir
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return hosts


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _finalize_pidfile_host(pid: int, socket_dir: Path, roots: tuple[Path, ...]) -> None:
    if not any(_under(socket_dir, root) for root in roots):
        return
    try:
        recorded_pid = int((socket_dir / "gterm.pid").read_text().strip())
    except (OSError, ValueError):
        return
    if recorded_pid != pid:
        return
    try:
        process = psutil.Process(pid)
        if _gterm_socket_dir(process.cmdline()) != socket_dir:
            return
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return


@pytest.fixture(scope="session", autouse=True)
def _assert_no_leaked_hosts(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    before = _gterm_hosts()
    yield

    run_tmp = os.environ.get("CLAUDE_CODE_TMPDIR")
    roots: tuple[Path, ...] = (tmp_path_factory.getbasetemp().resolve(),)
    if run_tmp:
        roots += (Path(run_tmp).resolve(),)
    after = _gterm_hosts()
    leaked = {
        pid: socket_dir
        for pid, socket_dir in after.items()
        if before.get(pid) != socket_dir and any(_under(socket_dir, root) for root in roots)
    }
    for pid, socket_dir in leaked.items():
        _finalize_pidfile_host(pid, socket_dir, roots)

    final = _gterm_hosts()
    remaining = {
        pid: socket_dir
        for pid, socket_dir in final.items()
        if before.get(pid) != socket_dir and any(_under(socket_dir, root) for root in roots)
    }
    assert not remaining, (
        "tests/terminals leaked gterm host processes that use this session's temp roots: "
        f"before={sorted(before)} after={sorted(final)} remaining={sorted(remaining)}"
    )


def gterm_binary() -> Path | None:
    """Return the first gterm binary on the isolated native-bin search path."""
    env = os.environ.get("GOBBY_NATIVE_BIN_DIR")
    if env:
        candidate = Path(env) / "gterm"
        if candidate.is_file():
            return candidate
    worktree = Path(__file__).resolve().parents[2]
    for directory in (
        worktree / "target" / "debug",
        worktree / ".gobby-native-bin",
        Path.home() / ".gobby" / "bin",
    ):
        candidate = directory / "gterm"
        if candidate.is_file():
            return candidate
    which = shutil.which("gterm")
    return Path(which) if which else None


def require_backend(backend: str) -> None:
    """Skip a contract cell when its real backend binary is absent."""
    if backend == "tmux" and shutil.which("tmux") is None:
        pytest.skip("tmux binary is not available")
    if backend == "native" and gterm_binary() is None:
        pytest.skip("gterm binary is not available")


@pytest.fixture(params=["tmux", "native"])
def contract_backend(request: pytest.FixtureRequest) -> str:
    backend = str(request.param)
    require_backend(backend)
    return backend
