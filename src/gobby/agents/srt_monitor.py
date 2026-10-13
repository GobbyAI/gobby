"""Start/reuse the host-owned SRT monitor without tying it to daemon lifetime."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

from gobby.agents.sandbox_policy import srt_mux_tmpdir
from gobby.utils.dependency_requirements import SRT_RELEASE

_lock = threading.Lock()
_children: list[subprocess.Popen[bytes]] = []


def _connect(path: Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(6)
        client.connect(str(path))
        client.sendall(b'{"suffix":"_gobbyprobe_SBX"}\n')
        with client.makefile("rb") as stream:
            record = json.loads(stream.readline(4096))
        if record != {"ready": True, "identity": SRT_RELEASE.runner_sha256}:
            raise RuntimeError("Shared SRT monitor identity mismatch; coordinate monitor cutover")


def ensure_srt_monitor(*, node: Path, runner: Path, env: Mapping[str, str]) -> str:
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
            child = subprocess.Popen(
                [str(node), str(runner), "--monitor", str(path), str(child_ready.fileno())],
                # The long-lived broker needs no per-run credentials, grants,
                # provider configuration, or Node preload options.
                env={"PATH": env.get("PATH", os.defpath)},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                pass_fds=(child_ready.fileno(),),
                start_new_session=True,
            )
            child_ready.close()
            _children[:] = [process for process in _children if process.poll() is None]
            _children.append(child)
            try:
                with parent.makefile("rb") as readiness:
                    if readiness.readline(16) != b"ready\n":
                        raise RuntimeError("Shared SRT monitor failed to become ready")
                _connect(path)
            except (OSError, ValueError, RuntimeError) as exc:
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
