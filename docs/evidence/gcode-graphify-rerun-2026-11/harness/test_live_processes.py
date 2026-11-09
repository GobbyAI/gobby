"""The live daemon listens on its public ports; its supervised backend listens on the backend port."""

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
WRAPPER_ARGV = [
    "/usr/local/bin/node",
    "srt_runner.mjs",
    "--",
    "/runtime/gobby-home/bin/gdaemon",
    "serve",
]
DAEMON_ARGV = ["/runtime/gobby-home/bin/gdaemon", "serve"]
BACKEND_ARGV = ["/runtime/tools/gobby/venv/bin/python", "-m", "gobby.runner"]
PUBLIC_LISTENS = (("127.0.0.1", 59480), ("127.0.0.1", 59481))
BACKEND_LISTENS = (("127.0.0.1", 59580),)
Runtime = tuple[Path, dict[str, Any], dict[str, str]]


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
def runtime(tmp_path: Path) -> Runtime:
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


def _backend(
    root: Path, environ: dict[str, str], *, supervised: bool = True, **kwargs: Any
) -> FakeProcess:
    backend_environ = {**environ, "GOBBY_SUPERVISOR_FD": "3"} if supervised else dict(environ)
    return FakeProcess(
        102, cwd=root / "gobby-home", cmdline=BACKEND_ARGV, environ=backend_environ, **kwargs
    )


def test_the_daemon_listens_on_the_public_ports_and_its_backend_on_the_backend_port(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    backend = _backend(root, environ, listens=BACKEND_LISTENS)
    daemon = _daemon(root, environ, listens=PUBLIC_LISTENS, children=[backend])
    _install(monkeypatch, _wrapper(root, environ, children=[daemon]))

    listeners = validate_live_processes(root, launches)

    assert [(item["role"], item["port"], item["pid"]) for item in listeners] == [
        ("public", 59480, 101),
        ("public", 59481, 101),
        ("backend", 59580, 102),
    ]


def test_a_wrapper_that_listens_itself_fails(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    backend = _backend(root, environ, listens=BACKEND_LISTENS)
    daemon = _daemon(root, environ, listens=PUBLIC_LISTENS, children=[backend])
    _install(monkeypatch, _wrapper(root, environ, listens=PUBLIC_LISTENS, children=[daemon]))

    with pytest.raises(EvidenceError, match="descendants"):
        validate_live_processes(root, launches)


def test_a_public_port_listened_on_by_a_stranger_fails(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    stranger = FakeProcess(
        103,
        cwd=root / "gobby-home",
        cmdline=["/usr/bin/nc", "-l", "59480"],
        environ=environ,
        listens=(("127.0.0.1", 59480),),
    )
    backend = _backend(root, environ, listens=BACKEND_LISTENS)
    daemon = _daemon(root, environ, listens=(("127.0.0.1", 59481),), children=[backend])
    _install(monkeypatch, _wrapper(root, environ, children=[stranger, daemon]))

    with pytest.raises(EvidenceError, match="not the daemon"):
        validate_live_processes(root, launches)


def test_a_backend_shaped_stray_outside_the_daemon_fails(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    stray = FakeProcess(
        103,
        cwd=root / "gobby-home",
        cmdline=BACKEND_ARGV,
        environ={**environ, "GOBBY_SUPERVISOR_FD": "3"},
        listens=BACKEND_LISTENS,
    )
    daemon = _daemon(root, environ, listens=PUBLIC_LISTENS)
    _install(monkeypatch, _wrapper(root, environ, children=[stray, daemon]))

    with pytest.raises(EvidenceError, match="not the daemon's backend"):
        validate_live_processes(root, launches)


def test_a_daemon_without_its_backend_listener_fails(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    daemon = _daemon(root, environ, listens=PUBLIC_LISTENS)
    _install(monkeypatch, _wrapper(root, environ, children=[daemon]))

    with pytest.raises(EvidenceError, match="not listened on by the wrapper's descendants"):
        validate_live_processes(root, launches)


def test_a_backend_without_the_supervisor_fd_fails(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    backend = _backend(root, environ, supervised=False, listens=BACKEND_LISTENS)
    daemon = _daemon(root, environ, listens=PUBLIC_LISTENS, children=[backend])
    _install(monkeypatch, _wrapper(root, environ, children=[daemon]))

    with pytest.raises(EvidenceError, match="supervisor fd"):
        validate_live_processes(root, launches)


def test_an_extra_listener_on_the_backend_control_port_fails(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    backend = _backend(root, environ, listens=(*BACKEND_LISTENS, ("127.0.0.1", 59581)))
    daemon = _daemon(root, environ, listens=PUBLIC_LISTENS, children=[backend])
    _install(monkeypatch, _wrapper(root, environ, children=[daemon]))

    with pytest.raises(EvidenceError, match="unexpected listener.*59581"):
        validate_live_processes(root, launches)


def test_an_unrelated_extra_listener_fails(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launches, environ = runtime
    backend = _backend(root, environ, listens=BACKEND_LISTENS)
    daemon = _daemon(
        root, environ, listens=(*PUBLIC_LISTENS, ("127.0.0.1", 9999)), children=[backend]
    )
    _install(monkeypatch, _wrapper(root, environ, children=[daemon]))

    with pytest.raises(EvidenceError, match="unexpected listener.*9999"):
        validate_live_processes(root, launches)


def test_a_daemon_listener_outside_the_runtime_root_fails(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, launches, environ = runtime
    backend = _backend(root, environ, listens=BACKEND_LISTENS)
    elsewhere = FakeProcess(
        101,
        cwd=tmp_path / "elsewhere",
        cmdline=DAEMON_ARGV,
        environ=environ,
        listens=PUBLIC_LISTENS,
        children=[backend],
    )
    _install(monkeypatch, _wrapper(root, environ, children=[elsewhere]))

    with pytest.raises(EvidenceError, match="outside the runtime root"):
        validate_live_processes(root, launches)
