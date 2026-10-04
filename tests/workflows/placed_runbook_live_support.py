"""Test-only support for the placed-runbook live acceptance (#23335).

The isolated daemon runs this module as its runner (``python -m``), reachable only
through the fixture's checkout-root PYTHONPATH. It wraps the genuine
``PipelineStepStorageMixin.update_step_execution`` with a one-shot completion
barrier and then delegates to ``gobby.runner``. Every write passes through except
the single armed step's COMPLETED write, which waits for the fixture's release.
It never fabricates a spawn reply, adoption, terminal or database row.

Barrier files, all in ``GOBBY_E2E_RUNBOOK_BARRIER_DIR`` so their consumed state
survives the runner's death:

- ``arm-request.json`` (fixture): one-use, names a step id. The matching RUNNING
  write consumes it and records ``arm-signal.json`` bound to that
  step_execution_id, then passes the write through.
- The COMPLETED write with exactly that step_execution_id atomically consumes the
  signal, records ``reached.json`` and waits for ``release`` before delegating.

Top-level imports stay stdlib and ``gobby``: the daemon imports this module.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psutil

from gobby.agents.srt_runtime import SrtRuntimeError, verify_srt_installation
from gobby.storage.pipeline_steps import PipelineStepStorageMixin
from gobby.terminals.host_client import HostClient
from gobby.terminals.host_protocol import (
    CONTROL_PROTOCOL_VERSION,
    HOST_LOG_NAME,
    control_socket_path,
    control_token_path,
    read_pidfile,
)
from gobby.utils.dependency_requirements import SRT_RELEASE
from gobby.workflows.pipeline_state import StepExecution, StepStatus

RUNNER_MODULE = "tests.workflows.placed_runbook_live_support"
BARRIER_DIR_ENV = "GOBBY_E2E_RUNBOOK_BARRIER_DIR"

ARM_REQUEST = "arm-request.json"
ARM_REQUEST_CONSUMED = "arm-request.consumed.json"
ARM_SIGNAL = "arm-signal.json"
ARM_SIGNAL_CONSUMED = "arm-signal.consumed.json"
REACHED = "reached.json"
RELEASE = "release.json"
RELEASE_CONSUMED = "release.consumed.json"
DISARMED_SUFFIX = ".disarmed"

# Only these spawn-reply fields are copied into the reached witness.
_HELD_FIELDS = ("success", "adopted", "run_id", "terminal_id", "tab_ref", "pane_ref")
# Real tools the isolated daemon and SRT need, linked beside the stand-ins.
_TOOLS = ("node", "git", "tmux", "rg")
_SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
# Evidence never carries credentials, prompts or provider argv.
_REDACTED_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "api_key",
    "argv",
    "cmdline",
    "command",
    "prompt",
    "environ",
)

_STANDIN = """\
#!/bin/sh
# Inert {provider} stand-in for the placed-runbook live acceptance (#23335).
# It does no model or planning work: it answers a version probe, prints one
# ready line and holds its terminal until the terminal closes.
case "${{1:-}}" in
  --version|-v|version) echo "{provider} 0.0.0-inert"; exit 0 ;;
esac
printf 'INERT-%s-READY %s\\n' "{provider}" "$$"
while IFS= read -r _line; do :; done
"""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise AssertionError(f"{path} does not hold a JSON object")
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staging.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(staging, path)


def _held_fields(output_json: str | None) -> dict[str, Any]:
    try:
        output = json.loads(output_json or "null")
    except json.JSONDecodeError:
        return {}
    if not isinstance(output, dict):
        return {}
    return {key: output[key] for key in _HELD_FIELDS if key in output}


def _arm(storage: PipelineStepStorageMixin, directory: Path, step_execution_id: int) -> None:
    """Consume the one-use request on the requested step's RUNNING write."""
    request = _read_json(directory / ARM_REQUEST)
    if request is None:
        return
    row = storage.db.fetchone(
        "SELECT execution_id, step_id FROM step_executions WHERE id = %s",
        (step_execution_id,),
    )
    if row is None or row["step_id"] != request["step_id"]:
        return
    try:
        os.replace(directory / ARM_REQUEST, directory / ARM_REQUEST_CONSUMED)
    except FileNotFoundError:
        return
    _write_json(
        directory / ARM_SIGNAL,
        {
            "execution_id": str(row["execution_id"]),
            "step_id": str(row["step_id"]),
            "step_execution_id": step_execution_id,
        },
    )


def _hold(directory: Path, step_execution_id: int, output_json: str | None) -> None:
    """Block the armed step's COMPLETED write until the fixture releases it."""
    signal = _read_json(directory / ARM_SIGNAL)
    if signal is None or signal["step_execution_id"] != step_execution_id:
        return
    try:
        os.replace(directory / ARM_SIGNAL, directory / ARM_SIGNAL_CONSUMED)
    except FileNotFoundError:
        return
    _write_json(
        directory / REACHED,
        {**signal, "runner_pid": os.getpid(), "held_output": _held_fields(output_json)},
    )
    # Unbounded on purpose: a timeout that delegated would commit the held write.
    while not (directory / RELEASE).exists():
        time.sleep(0.05)
    os.replace(directory / RELEASE, directory / RELEASE_CONSUMED)


def barrier_update(
    directory: Path,
    original: Callable[..., StepExecution | None],
) -> Callable[..., StepExecution | None]:
    """Wrap the genuine step write; only the armed COMPLETED write blocks."""

    def update_step_execution(
        self: PipelineStepStorageMixin,
        step_execution_id: int,
        status: StepStatus | None = None,
        output_json: str | None = None,
        error: str | None = None,
        approval_token: str | None = None,
        approved_by: str | None = None,
        approval_timeout_seconds: int | None = None,
    ) -> StepExecution | None:
        if status is StepStatus.RUNNING:
            _arm(self, directory, step_execution_id)
        elif status is StepStatus.COMPLETED:
            _hold(directory, step_execution_id, output_json)
        return original(
            self,
            step_execution_id,
            status=status,
            output_json=output_json,
            error=error,
            approval_token=approval_token,
            approved_by=approved_by,
            approval_timeout_seconds=approval_timeout_seconds,
        )

    return update_step_execution


@dataclass(frozen=True)
class Barrier:
    """The fixture's side of the one-shot completion barrier."""

    directory: Path

    def path(self, name: str) -> Path:
        return self.directory / name

    def arm(self, step_id: str) -> None:
        names = (ARM_REQUEST, ARM_REQUEST_CONSUMED, ARM_SIGNAL, ARM_SIGNAL_CONSUMED, REACHED)
        used = [name for name in names if self.path(name).exists()]
        assert not used, f"the one-shot barrier was already used: {used}"
        _write_json(self.path(ARM_REQUEST), {"step_id": step_id})

    def reached(self) -> dict[str, Any] | None:
        return _read_json(self.path(REACHED))

    def signal(self) -> dict[str, Any] | None:
        return _read_json(self.path(ARM_SIGNAL_CONSUMED))

    def consumed(self) -> bool:
        """Both one-use signals are gone and each left its consumed witness."""
        return (
            not self.path(ARM_REQUEST).exists()
            and not self.path(ARM_SIGNAL).exists()
            and self.path(ARM_REQUEST_CONSUMED).exists()
            and self.path(ARM_SIGNAL_CONSUMED).exists()
        )

    def release(self) -> None:
        _write_json(self.path(RELEASE), {"released_at": time.time()})

    def released(self) -> bool:
        return self.path(RELEASE_CONSUMED).exists()

    def cleanup(self) -> dict[str, bool]:
        """Disarm unconsumed signals and release a barrier a live runner still holds."""
        disarmed = {}
        for name in (ARM_REQUEST, ARM_SIGNAL):
            try:
                os.replace(self.path(name), self.path(name + DISARMED_SUFFIX))
                disarmed[name] = True
            except FileNotFoundError:
                disarmed[name] = False
        held = self.path(REACHED).exists() and not self.released()
        if held and not self.path(RELEASE).exists():
            self.release()
        return {**disarmed, "released_held_write": held}


def write_standin(directory: Path, provider: str) -> Path:
    path = directory / provider
    path.write_text(_STANDIN.format(provider=provider), encoding="utf-8")
    path.chmod(0o755)
    return path


def set_executable(path: Path, executable: bool) -> None:
    """The deliberate failure mode: a stand-in without execute bits is not resolvable."""
    mode = stat.S_IMODE(path.stat().st_mode)
    exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    path.chmod(mode | exec_bits if executable else mode & ~exec_bits)


def curated_path(root: Path, providers: Iterable[str]) -> tuple[str, dict[str, Path]]:
    """Stand-ins first, then links to real tools, then system dirs; no real provider."""
    standins = root / "providers"
    tools = root / "tools"
    standins.mkdir()
    tools.mkdir()
    for tool in _TOOLS:
        real = shutil.which(tool)
        if real is not None:
            (tools / tool).symlink_to(real)
    assert (tools / "node").exists(), "managed SRT needs node on the test PATH"
    fallback = os.pathsep.join([str(tools), *_SYSTEM_PATH])
    path = os.pathsep.join([str(standins), fallback])
    created: dict[str, Path] = {}
    for provider in sorted(set(providers)):
        # Without this, a non-executable stand-in would fall through to a real CLI.
        assert shutil.which(provider, path=fallback) is None, f"a real {provider} is on PATH"
        created[provider] = write_standin(standins, provider)
        assert shutil.which(provider, path=path) == str(created[provider])
    return path, created


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage_real_srt(home: Path) -> dict[str, str]:
    """Copy the operator's pinned SRT install byte-for-byte and verify it under ``home``.

    A missing or failing install is a failed precondition, never a skip.
    """
    operator = Path.home() / ".gobby" / "tools" / "srt" / SRT_RELEASE.version
    if not operator.is_dir():
        raise AssertionError(f"precondition 6: managed SRT {SRT_RELEASE.version} is missing")
    staged = home / "tools" / "srt" / SRT_RELEASE.version
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(operator, staged, symlinks=True)
    previous = os.environ.get("GOBBY_HOME")
    os.environ["GOBBY_HOME"] = str(home)
    try:
        installation = verify_srt_installation()
    except (OSError, SrtRuntimeError) as exc:
        raise AssertionError(f"precondition 6: staged SRT failed verification: {exc}") from exc
    finally:
        if previous is None:
            os.environ.pop("GOBBY_HOME", None)
        else:
            os.environ["GOBBY_HOME"] = previous
    assert installation.root.resolve() == staged.resolve(), installation.root
    return {
        "version": SRT_RELEASE.version,
        "root": str(staged),
        "runner_sha256": sha256_file(installation.runner),
        "receipt_sha256": sha256_file(staged / "receipt.json"),
        "package_lock_sha256": sha256_file(staged / "package-lock.json"),
        "node": str(installation.node),
    }


@dataclass(frozen=True)
class HostIdentity:
    pid: int
    epoch: str


@dataclass
class FixtureHost:
    """A gterm host the fixture owns; the isolated daemon adopts it."""

    socket_dir: Path
    process: subprocess.Popen[bytes]
    create_time: float

    @property
    def pid(self) -> int:
        return self.process.pid

    def alive(self) -> bool:
        return self.process.poll() is None


def start_fixture_host(binary: Path, socket_dir: Path, env: Mapping[str, str]) -> FixtureHost:
    """Start ``gterm host`` in its own session with the daemon's environment."""
    log_dir = socket_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    host_env = {key: value for key, value in env.items() if key not in ("TMUX", "TMUX_PANE")}
    host_env["GTERM_LOG_FILE"] = str(log_dir / HOST_LOG_NAME)
    with (log_dir / HOST_LOG_NAME).open("ab") as log:
        process = subprocess.Popen(
            [str(binary), "host", "--socket-dir", str(socket_dir)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=host_env,
            start_new_session=True,
        )
    deadline = time.monotonic() + 30.0
    while not (
        control_socket_path(socket_dir).exists() and read_pidfile(socket_dir) == process.pid
    ):
        if process.poll() is not None:
            raise AssertionError(f"fixture gterm host exited with {process.returncode}")
        assert time.monotonic() < deadline, "fixture gterm host never served its socket"
        time.sleep(0.05)
    return FixtureHost(socket_dir, process, psutil.Process(process.pid).create_time())


async def _control(socket_dir: Path) -> HostClient:
    token = control_token_path(socket_dir).read_text(encoding="utf-8").strip()
    client = await HostClient.connect(control_socket_path(socket_dir))
    await client.hello(CONTROL_PROTOCOL_VERSION, token)
    return client


async def _ping(socket_dir: Path) -> HostIdentity:
    client = await _control(socket_dir)
    try:
        ping = await client.ping()
    finally:
        await client.close()
    return HostIdentity(pid=int(ping.host_pid), epoch=str(ping.host_epoch))


def host_identity(socket_dir: Path) -> HostIdentity:
    """Ask the host itself who it is; the daemon's health reports no host pid."""
    return asyncio.run(_ping(socket_dir))


async def _shutdown(socket_dir: Path) -> None:
    client = await _control(socket_dir)
    try:
        await client.host_shutdown(grace_ms=1000)
    finally:
        await client.close()


def stop_fixture_host(host: FixtureHost) -> dict[str, Any]:
    """Shut down only this fixture's host, through its socket, then its own handle."""
    if not host.alive():
        return {"pid": host.pid, "already_exited": True}
    method = "host_shutdown"
    try:
        asyncio.run(_shutdown(host.socket_dir))
        host.process.wait(timeout=15)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        method = "kill"
    # The host runs in its own session, so nothing else would reap it.
    if host.alive():
        method = "kill"
        host.process.kill()
        host.process.wait(timeout=15)
    return {"pid": host.pid, "already_exited": False, "method": method}


def sanitize(value: Any) -> Any:
    """JSON-safe evidence with credential, prompt and argv keys removed."""
    if isinstance(value, Mapping):
        return {
            str(key): sanitize(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _REDACTED_KEY_PARTS)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize(item) for item in value]
    if isinstance(value, (Path, datetime)):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def write_evidence(directory: Path, name: str, payload: Mapping[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(sanitize(payload), indent=2, sort_keys=True), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Placed-runbook live acceptance runner")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    directory = Path(os.environ[BARRIER_DIR_ENV])

    from gobby.runner import main as run

    wrapper = barrier_update(directory, PipelineStepStorageMixin.update_step_execution)
    with patch.object(PipelineStepStorageMixin, "update_step_execution", wrapper):
        run(config_path=args.config, verbose=args.verbose)


if __name__ == "__main__":
    main()
