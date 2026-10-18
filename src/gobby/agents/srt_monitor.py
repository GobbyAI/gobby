"""Start/reuse the host-owned SRT monitor without tying it to daemon lifetime."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

import psutil

from gobby.agents.srt_socket_paths import srt_mux_tmpdir
from gobby.paths import get_gobby_home
from gobby.utils.dependency_requirements import SRT_RELEASE
from gobby.utils.spawn import run

_lock = threading.Lock()
_children: list[subprocess.Popen[bytes]] = []
_child_started: dict[int, float] = {}
# The owner record beside the socket names a broker a restart must preserve. It is a
# claim, not proof: _verified_recorded_broker checks each pinned field on the live process.
_RECORD_SUFFIX = ".owner.json"
_RECORD_LIMIT = 65536
_SOL_LOCAL = 0  # macOS sys/un.h
_LOCAL_PEERPID = 2  # macOS sys/un.h
_SO_PEERCRED = 17  # Linux asm-generic/socket.h


def live_monitor_identities() -> dict[int, float]:
    """Return only live brokers whose creation identity still matches our child or record."""
    with _lock:
        identities: dict[int, float] = {}
        for child in _children:
            if child.poll() is not None:
                continue
            started = _child_started.get(child.pid)
            try:
                if started is not None and psutil.Process(child.pid).create_time() == started:
                    identities[child.pid] = started
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        recorded = _verified_recorded_broker()
        if recorded is not None:
            broker_pid, broker_started = recorded
            identities.setdefault(broker_pid, broker_started)
        return identities


def _verified_recorded_broker() -> tuple[int, float] | None:
    """Return the owner-recorded broker only when every pinned field still holds.

    Same-uid code can write the record, so the broker must answer the probe as the
    listener of this socket and match the live process on every field.
    """
    directory = srt_mux_tmpdir()
    path = directory / f"v-{SRT_RELEASE.runner_sha256[:12]}"
    record = _read_owner_record(path)
    if record is None:
        return None
    try:
        pid = record["pid"]
        started = float(record["started"])
        node = record["node"]
        argv = record["argv"]
        sha256 = record["sha256"]
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(node, str)
        or not isinstance(argv, list)
        or sha256 != SRT_RELEASE.runner_sha256
        or not _argv_is_pinned(argv, node, path)
    ):
        return None
    try:
        listener = _connect(path)
    except (OSError, ValueError, RuntimeError):
        return None
    if listener != pid:
        return None
    try:
        process = psutil.Process(pid)
        if (
            process.create_time() != started
            or os.path.realpath(process.exe()) != os.path.realpath(node)
            or process.cmdline() != argv
            or process.uids().real != os.getuid()
        ):
            return None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    return pid, started


def _read_owner_record(path: Path) -> dict[str, Any] | None:
    """Read the owner record beside the socket without following links."""
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(
            os.open(path.name + _RECORD_SUFFIX, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd), "rb"
        ) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return None
            record = json.loads(handle.read(_RECORD_LIMIT))
    except (OSError, ValueError):
        return None
    finally:
        os.close(dir_fd)
    return record if isinstance(record, dict) else None


def _write_owner_record(path: Path, record: Mapping[str, object]) -> None:
    """Replace the owner record atomically through an owner-only directory fd.

    The caller holds violations.lock, which serializes writers.
    """
    name = path.name + _RECORD_SUFFIX
    temporary = f"{name}.{os.getpid()}.tmp"
    payload = json.dumps(record).encode()
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=dir_fd)
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd
        )
        with os.fdopen(fd, "wb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("Unsafe SRT monitor owner record")
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _violations_dir() -> Path:
    return get_gobby_home() / "logs" / "sandbox-violations"


def _monitor_argv(*, node: Path, runner: Path, path: Path, ready_fd: int) -> list[str]:
    return [str(node), str(runner), "--monitor", str(path), str(ready_fd), str(_violations_dir())]


def _argv_is_pinned(argv: list[Any], node: str, path: Path) -> bool:
    """Require the exact _monitor_argv shape for this socket and violations directory."""
    if len(argv) != 6 or not all(isinstance(item, str) for item in argv):
        return False
    return (
        argv[0] == node
        and Path(node).is_absolute()
        and Path(argv[1]).is_absolute()
        and argv[2:4] == ["--monitor", str(path)]
        and argv[4].isdigit()
        and argv[5] == str(_violations_dir())
    )


def _connect(path: Path) -> int | None:
    """Probe the broker and return the pid listening on its socket, or None if unprovable."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(str(path))
        client.sendall(b'{"suffix":"_gobbyprobe_SBX"}\n')
        with client.makefile("rb") as stream:
            record = json.loads(stream.readline(4096))
        if record != {"ready": True, "identity": SRT_RELEASE.runner_sha256}:
            raise RuntimeError("Shared SRT monitor identity mismatch; coordinate monitor cutover")
        return _listener_pid(client)


def _listener_pid(client: socket.socket) -> int | None:
    """Return the pid that owns the listening end of a connected unix socket."""
    if sys.platform == "darwin":
        raw = client.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("i"))
        return int(struct.unpack("i", raw)[0])
    if sys.platform.startswith("linux"):
        raw = client.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i"))
        return int(struct.unpack("3i", raw)[0])
    return None


def _start_monitor(
    *, argv: list[str], ready_fd: int, env: Mapping[str, str], log: BinaryIO
) -> subprocess.Popen[bytes]:
    # One broker per runner identity needs a private readiness descriptor and
    # its own session. Restart reaping explicitly preserves its process identity;
    # setsid alone does not remove daemon ancestry. spawn.popen rejects pass_fds
    # and start_new_session; keep this rare fork exception separate from recovery.
    return subprocess.Popen(
        argv,
        # No per-run credentials, grants, provider configuration or Node preloads.
        env={"PATH": env.get("PATH", os.defpath)},
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        pass_fds=(ready_fd,),
        start_new_session=True,
    )


def ensure_srt_monitor(
    *, node: Path, runner: Path, env: Mapping[str, str], run_key: str | None = None
) -> str:
    """Serialize startup across processes and prove monitor identity/readiness.

    The private socket is outside managed execution grants. A live incompatible
    broker is never replaced under its subscribers.
    """
    directory = srt_mux_tmpdir()
    # Version-addressed sockets let the old cohort drain during a component
    # update without killing its monitoring or blocking the new cohort.
    path = directory / f"v-{SRT_RELEASE.runner_sha256[:12]}"
    with _lock, (directory / "violations.lock").open("a+b") as lock_file:
        os.chmod(lock_file.name, 0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if run_key is not None:
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", run_key) is None:
                raise ValueError("Invalid shared SRT monitor run key")
            prior = [
                directory / f"{run_key}.srt-backlog.jsonl{suffix}" for suffix in ("", ".active")
            ]
            if any(path.exists() or path.is_symlink() for path in prior):
                descriptor = os.open(
                    directory / "violations-monitor.log",
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(descriptor, "ab") as log:
                    try:
                        result = run(
                            [
                                str(node),
                                str(runner),
                                "--recover",
                                str(directory),
                                str(get_gobby_home() / "logs" / "sandbox-violations"),
                                run_key,
                            ],
                            env={"PATH": env.get("PATH", os.defpath)},
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=log,
                            timeout=30,
                            check=False,
                        )
                    except subprocess.TimeoutExpired as exc:
                        raise RuntimeError(
                            f"SRT backlog recovery timed out for run {run_key}"
                        ) from exc
                if result.returncode == 2:
                    raise RuntimeError(f"Run {run_key} has a backlog owned by a live SRT monitor")
                if result.returncode != 0:
                    raise RuntimeError(
                        f"SRT backlog recovery failed for run {run_key}; inspect {directory / 'violations-monitor.log'}"
                    )
        try:
            _connect(path)
            return str(path)
        except (FileNotFoundError, ConnectionRefusedError):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise RuntimeError("Unsafe shared SRT monitor socket") from None
                path.unlink()
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Shared SRT monitor at {path} is unresponsive; inspect "
                f"{directory / 'violations-monitor.log'} and coordinate recovery of its cohort"
            ) from exc

        descriptor = os.open(
            directory / "violations-monitor.log",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        parent, child_ready = socket.socketpair()
        with os.fdopen(descriptor, "ab") as log, parent, child_ready:
            parent.settimeout(30)
            argv = _monitor_argv(node=node, runner=runner, path=path, ready_fd=child_ready.fileno())
            child = _start_monitor(
                argv=argv,
                ready_fd=child_ready.fileno(),
                env=env,
                log=log,
            )
            child_ready.close()
            _children[:] = [process for process in _children if process.poll() is None]
            live_pids = {process.pid for process in _children}
            for pid in list(_child_started):
                if pid not in live_pids:
                    del _child_started[pid]
            _children.append(child)
            try:
                started = psutil.Process(child.pid).create_time()
                _child_started[child.pid] = started
                with parent.makefile("rb") as readiness:
                    if readiness.readline(16) != b"ready\n":
                        raise RuntimeError("Shared SRT monitor failed to become ready")
                _connect(path)
                _write_owner_record(
                    path,
                    {
                        "pid": child.pid,
                        "started": repr(started),
                        "node": argv[0],
                        "argv": argv,
                        "sha256": SRT_RELEASE.runner_sha256,
                    },
                )
            except (
                OSError,
                ValueError,
                RuntimeError,
                psutil.NoSuchProcess,
                psutil.AccessDenied,
            ) as exc:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                diagnostic = (directory / "violations-monitor.log").read_text()[-4096:]
                raise RuntimeError(
                    f"Shared SRT monitor startup failed ({exc}); inspect {directory / 'violations-monitor.log'}: "
                    f"{diagnostic}"
                ) from exc
        return str(path)
