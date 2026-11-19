"""One owner per Python start: the full runner or the worker serve loop, never both.

Each case runs a fresh interpreter. The driver patches the start's external dependencies
and records which owner the entry point constructs. A marked start runs only the worker.
An unmarked start runs only the full runner; that is the pre CP-RUNNER-CUT behavior.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path

import pytest

import gobby
from gobby.worker.channel import WORKER_CHANNEL_FD_ENV, WORKER_CREDENTIAL_FD_ENV, WORKER_MARKER_ENV

pytestmark = pytest.mark.integration

REFUSED_EXIT_CODE = 78
PROCESS_TIMEOUT_SECONDS = 120
SRC_ROOT = Path(gobby.__file__).resolve().parents[1]
EVENTS_FILE = "events.txt"

# Runs in a child interpreter. Each external dependency is replaced by a recorder or a
# no-op before the real entry point runs; the full runner and the worker serve loop
# each append their name to the event file when they are constructed.
DRIVER = textwrap.dedent(
    r"""
    import os
    import sys

    import gobby.providers.version_gate as version_gate
    import gobby.runner as runner
    import gobby.runner_backend as runner_backend
    import gobby.runtime_grants.lease_view as lease_view
    import gobby.storage.schema_contract as schema_contract
    import gobby.utils.break_glass as break_glass
    import gobby.worker.channel as worker_channel
    from gobby.runner_init import StartRefusal

    EVENTS_PATH = os.path.join(os.environ["GOBBY_HOME"], "events.txt")


    def record(event):
        with open(EVENTS_PATH, "a", encoding="utf-8") as handle:
            handle.write(event + "\n")


    def quiet(*args, **kwargs):
        return None


    async def quiet_async(*args, **kwargs):
        return None


    class QuietLeaseView:
        def __init__(self, *args, **kwargs):
            pass

        def bind(self):
            return None

        def release(self):
            return None


    async def create_full_runner(*args, **kwargs):
        record("runner-create")
        raise StartRefusal("driver stopped the full runner before it started")


    async def serve_worker_only(boot):
        record("worker-serve")


    schema_contract.verify_schema = quiet
    break_glass.ensure_break_glass_credential = quiet
    lease_view.RuntimeLeaseView = QuietLeaseView
    version_gate.probe_and_publish_agy_support = quiet_async
    runner_backend.watch_supervisor = quiet_async
    runner.GobbyRunner.create = create_full_runner
    worker_channel.serve_worker = serve_worker_only

    try:
        runner.main()
    except SystemExit as exc:
        sys.exit(exc.code)
    """
)


def _credential_descriptor(payload: bytes) -> int:
    """Return the read end of a pipe holding payload, with its write end already closed."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    return read_fd


def _close_quietly(fd: int) -> None:
    """Close a descriptor that the child may already have consumed in a different process."""
    with contextlib.suppress(OSError):
        os.close(fd)


def _write_home(tmp_path: Path) -> tuple[Path, str]:
    database_url = os.environ.get("DATABASE_URL")
    assert database_url, "DATABASE_URL must name the isolated test hub"
    home = tmp_path / "gobby-home"
    home.mkdir()
    files_home = tmp_path / "files-home"
    files_home.mkdir()
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(
        f"database_url: {json.dumps(database_url)}\nfiles_home: {json.dumps(str(files_home))}\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)
    return home, database_url


def _start_environment(home: Path, database_url: str, boot: Mapping[str, str]) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GOBBY_")}
    environment.update(
        {
            "DATABASE_URL": database_url,
            "GOBBY_HOME": str(home),
            "GOBBY_TEST_PROTECT": "1",
            "GOBBY_ALLOW_WORKTREE_DAEMON": "1",
            "PYTHONPATH": str(SRC_ROOT),
            **boot,
        }
    )
    return environment


def _run_start(
    home: Path,
    database_url: str,
    boot: Mapping[str, str],
    pass_fds: tuple[int, ...],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", DRIVER],
        capture_output=True,
        text=True,
        env=_start_environment(home, database_url, boot),
        pass_fds=pass_fds,
        cwd=home,
        timeout=PROCESS_TIMEOUT_SECONDS,
        check=False,
    )


def _events(home: Path) -> list[str]:
    path = home / EVENTS_FILE
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def test_marker_present_runs_only_the_worker(tmp_path: Path) -> None:
    home, database_url = _write_home(tmp_path)
    credential_fd = _credential_descriptor(b"driver-credential")
    channel, peer = socket.socketpair()
    try:
        boot = {
            WORKER_MARKER_ENV: "1",
            WORKER_CHANNEL_FD_ENV: str(channel.fileno()),
            WORKER_CREDENTIAL_FD_ENV: str(credential_fd),
        }
        _run_start(home, database_url, boot, pass_fds=(channel.fileno(), credential_fd))
    finally:
        _close_quietly(credential_fd)
        channel.close()
        peer.close()
    assert _events(home) == ["worker-serve"]


def test_marker_without_descriptor_exits_78_before_any_owner(tmp_path: Path) -> None:
    home, database_url = _write_home(tmp_path)
    credential_fd = _credential_descriptor(b"driver-credential")
    try:
        boot = {WORKER_MARKER_ENV: "1", WORKER_CREDENTIAL_FD_ENV: str(credential_fd)}
        result = _run_start(home, database_url, boot, pass_fds=(credential_fd,))
    finally:
        _close_quietly(credential_fd)
    assert result.returncode == REFUSED_EXIT_CODE, result.stderr
    assert WORKER_CHANNEL_FD_ENV in result.stderr
    assert _events(home) == []


def test_marker_absent_runs_only_the_full_runner(tmp_path: Path) -> None:
    """pre CP-RUNNER-CUT: with no marker, the full runner is the one owner that is constructed."""
    home, database_url = _write_home(tmp_path)
    result = _run_start(home, database_url, {}, pass_fds=())
    assert result.returncode == REFUSED_EXIT_CODE, result.stderr
    assert _events(home) == ["runner-create"]
