"""The daemon's listeners are descendants of the SRT wrapper the launch receipt names."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from runtime_boundary import EvidenceError, file_record
from validate_environment import validate_live_processes

PATH = "/usr/bin:/bin"
WRAPPER_ARGV = ["/usr/local/bin/node", "srt_runner.mjs", "--", "python", "-m", "gobby.runner"]
DAEMON_ARGV = ["/usr/bin/python3", "-m", "gobby.runner", "--config", "gobby-home/bootstrap.yaml"]
LISTENS = (("127.0.0.1", 59480), ("127.0.0.1", 59481))


class FakeProcess:
    def __init__(
        self,
        pid: int,
        *,
        cwd: Path | str,
        cmdline: list[str],
        environ: dict[str, str],
        listens: tuple[tuple[str, int], ...] = (),
        children: list[FakeProcess] | None = None,
    ) -> None:
        self.pid = pid
        self._cwd = str(cwd)
        self._cmdline = list(cmdline)
        self._environ = dict(environ)
        self._listens = listens
        self._children = children or []

    def uids(self) -> SimpleNamespace:
        return SimpleNamespace(real=os.getuid())

    def cwd(self) -> str:
        return self._cwd

    def cmdline(self) -> list[str]:
        return list(self._cmdline)

    def environ(self) -> dict[str, str]:
        return dict(self._environ)

    def create_time(self) -> float:
        return 1000.0 + self.pid

    def children(self, recursive: bool = False) -> list[FakeProcess]:
        found = list(self._children)
        if recursive:
            for child in self._children:
                found.extend(child.children(recursive=True))
        return found

    def net_connections(self, kind: str = "tcp") -> list[SimpleNamespace]:
        return [
            SimpleNamespace(laddr=SimpleNamespace(ip=ip, port=port), status=psutil.CONN_LISTEN)
            for ip, port in self._listens
        ]


@pytest.fixture
def runtime(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, str]]:
    root = tmp_path / "runtime"
    home = root / "gobby-home"
    home.mkdir(parents=True)
    (root / "receipts").mkdir()
    receipt = root / "receipts/daemon-launch-attempt-1.json"
    receipt.write_text(
        json.dumps(
            {
                "pid": 100,
                "argv": WRAPPER_ARGV,
                "daemon_argv": DAEMON_ARGV,
                "cwd": str(home),
                "path": PATH,
            }
        )
    )
    environ = {
        "GOBBY_HOME": str(home),
        "PATH": PATH,
        "GOBBY_NATIVE_BIN_DIR": str(home / "bin"),
    }
    return root, {"daemon": file_record(root, receipt)}, environ


def _install(monkeypatch: pytest.MonkeyPatch, wrapper: FakeProcess) -> None:
    table = {wrapper.pid: wrapper}
    stack = list(wrapper.children(recursive=True))
    while stack:
        process = stack.pop()
        table[process.pid] = process
        stack.extend(process.children(recursive=True))
    monkeypatch.setattr(psutil, "Process", lambda pid: table[pid])


def _wrapper(root: Path, environ: dict[str, str], **kwargs: Any) -> FakeProcess:
    return FakeProcess(
        100, cwd=root / "gobby-home", cmdline=WRAPPER_ARGV, environ=environ, **kwargs
    )


def _daemon(root: Path, environ: dict[str, str], **kwargs: Any) -> FakeProcess:
    return FakeProcess(101, cwd=root / "gobby-home", cmdline=DAEMON_ARGV, environ=environ, **kwargs)


def test_a_wrapper_without_listeners_passes_when_its_descendants_listen(
    runtime: tuple[Path, dict[str, Any], dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    daemon = _daemon(root, environ, listens=LISTENS)
    _install(monkeypatch, _wrapper(root, environ, children=[daemon]))

    listeners = validate_live_processes(root, launches)

    assert [(item["port"], item["pid"]) for item in listeners] == [(59480, 101), (59481, 101)]


def test_a_wrapper_that_listens_itself_fails(
    runtime: tuple[Path, dict[str, Any], dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    _install(monkeypatch, _wrapper(root, environ, listens=LISTENS))

    with pytest.raises(EvidenceError, match="descendants"):
        validate_live_processes(root, launches)


def test_a_descendant_listener_that_is_not_the_daemon_fails(
    runtime: tuple[Path, dict[str, Any], dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    stranger = FakeProcess(
        102,
        cwd=root / "gobby-home",
        cmdline=["/usr/bin/nc", "-l", "59480"],
        environ=environ,
        listens=(("127.0.0.1", 59480),),
    )
    daemon = _daemon(root, environ, listens=(("127.0.0.1", 59481),))
    _install(monkeypatch, _wrapper(root, environ, children=[stranger, daemon]))

    with pytest.raises(EvidenceError, match="not the daemon"):
        validate_live_processes(root, launches)


def test_a_daemon_listener_outside_the_runtime_root_fails(
    runtime: tuple[Path, dict[str, Any], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root, launches, environ = runtime
    elsewhere = FakeProcess(
        101, cwd=tmp_path / "elsewhere", cmdline=DAEMON_ARGV, environ=environ, listens=LISTENS
    )
    _install(monkeypatch, _wrapper(root, environ, children=[elsewhere]))

    with pytest.raises(EvidenceError, match="outside the runtime root"):
        validate_live_processes(root, launches)
