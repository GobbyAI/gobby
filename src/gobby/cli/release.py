"""`gobby release batch`: run one release batch for the caller's live window."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess  # nosec B404 # argv-form activation through the spawn helper
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import click
import httpx

from gobby.cli.utils import get_project_context

if TYPE_CHECKING:
    from gobby.releases.batch import (
        ActivationKind,
        BatchEnvironment,
        BatchRecord,
        Check,
        CheckResult,
    )
    from gobby.releases.smoke import SmokeResult
    from gobby.releases.store import DaemonReleaseLease

_CHECK_TIMEOUT_SECONDS = 3600
_ACTIVATION_TIMEOUT_SECONDS = 3600
_SMOKE_DEADLINE_SECONDS = 300.0
_SET_BINARIES = ("gcode", "gdaemon", "ghook")


@click.group()
def release() -> None:
    """Validate, activate and smoke the release window's pinned candidate."""


@release.command("batch")
@click.option("--token", required=True, help="The live release window token.")
@click.option("--generation", type=int, required=True, help="The window's current generation.")
@click.option("--base-sha", default=None, help="Baseline to diff from when no batch has succeeded.")
def batch(token: str, generation: int, base_sha: str | None) -> None:
    """Run the batch in the foreground; the Release Manager starts it in the background."""
    from gobby.releases.batch import ReleaseBatchRefused, ReleaseStoreUnavailable
    from gobby.releases.store import DaemonReleaseLease
    from gobby.releases.window import ReleaseWindowRefused

    project = get_project_context()
    owner = os.environ.get("GOBBY_SESSION_ID", "").strip()
    if not project or not owner:
        raise click.ClickException("run inside a project as a session with GOBBY_SESSION_ID")
    lease = DaemonReleaseLease(
        _lease_call, owner_session_id=owner, token=token, generation=generation
    )
    try:
        record = asyncio.run(_seat_batch(lease, base_sha))
    except (ReleaseBatchRefused, ReleaseWindowRefused, ReleaseStoreUnavailable) as refused:
        click.echo(f"{refused.code}: {refused}", err=True)
        sys.exit(1)
    click.echo(
        json.dumps(
            {
                "status": record.status,
                "phase": record.phase,
                "candidate_sha": record.candidate_sha,
                "regression_task": record.regression_task,
                "diagnostics": list(record.diagnostics),
            },
            indent=2,
        )
    )
    sys.exit(0 if record.status == "succeeded" else 1)


async def _seat_batch(lease: DaemonReleaseLease, base_sha: str | None) -> BatchRecord:
    """Run the batch over the daemon's checkout; every release read crosses the lease."""
    from gobby.releases.batch import run_batch

    checkout = await lease.checkout()
    return await run_batch(lease, base_sha=base_sha, env=_environment(checkout.root, lease.token))


async def _lease_call(
    session_id: str, server: str, tool: str, arguments: dict[str, object]
) -> tuple[int, object]:
    """One daemon tool call made as the Release Manager session, off the event loop."""
    return await asyncio.to_thread(_DaemonProbe(session_id).call_tool, server, tool, arguments)


def _environment(root: Path, token: str) -> BatchEnvironment:
    from gobby.hooks.verification_runner import run_command
    from gobby.releases.batch import BatchEnvironment, CheckResult
    from gobby.releases.smoke import run_smokes
    from gobby.utils.project_context import get_verification_config

    config = get_verification_config(root)

    def run_check(check: Check) -> CheckResult:
        result = run_command(check.name, check.command, root, timeout=_CHECK_TIMEOUT_SECONDS)
        output = "\n".join(part for part in (result.stdout, result.stderr, result.error) if part)
        return CheckResult(check.name, check.command, result.success, output)

    def smoke() -> list[SmokeResult]:
        # The deadline starts when the smokes do, not when the batch builds its environment.
        return run_smokes(
            _DaemonProbe(),
            schema_pin=lambda: _schema_pin_mismatch(root),
            deadline=time.monotonic() + _SMOKE_DEADLINE_SECONDS,
        )

    return BatchEnvironment(
        command=config.get_command if config is not None else lambda _name: None,
        run_check=run_check,
        activation_kind=lambda: _activation_kind(root),
        activate=lambda kind: _activate(root, token, kind),
        smoke=smoke,
        announce=_announce,
    )


def _activation_kind(root: Path) -> ActivationKind:
    """Cut over when any stamped binary's workspace source differs from its install."""
    from gobby.install.bin_release_identity import release_identity_path, workspace_source_identity
    from gobby.utils.native_bin import native_bin_dir

    bin_dir = native_bin_dir()
    for name in _SET_BINARIES:
        try:
            installed: object = json.loads(release_identity_path(bin_dir, name).read_text())
        except (OSError, ValueError):
            return "cutover"
        source = installed.get("source_identity") if isinstance(installed, dict) else None
        if source != workspace_source_identity(root, name):
            return "cutover"
    return "restart"


def _activate(root: Path, token: str, kind: ActivationKind) -> None:
    """Run the existing command from the main checkout; its own fences re-prove the lease."""
    from gobby.releases.batch import ActivationFailed
    from gobby.utils import spawn
    from gobby.utils.local_token import GOBBY_RELEASE_TOKEN_ENV

    # Only the activation child carries the token; checks never see it.
    env = {**os.environ, GOBBY_RELEASE_TOKEN_ENV: token}
    try:
        result = spawn.run(
            [sys.executable, "-m", "gobby.cli", kind, "--wait"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=_ACTIVATION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActivationFailed(f"{kind} did not finish: {exc}") from exc
    if result.returncode != 0:
        raise ActivationFailed(f"{kind} exited {result.returncode}: {result.stdout}{result.stderr}")


def _schema_pin_mismatch(root: Path) -> str | None:
    from gobby.cli.cutover import CutoverError, read_installed_pin, read_workspace_pin
    from gobby.utils.native_bin import native_bin_dir

    try:
        installed = read_installed_pin(native_bin_dir())
        workspace = read_workspace_pin(root)
    except CutoverError as exc:
        return str(exc)
    return None if installed == workspace else f"installed {installed} != workspace {workspace}"


class _DaemonProbe:
    """Daemon calls over the CLI client; status 0 when the daemon does not answer.

    ``session_id`` names the session a tool call acts as, so the daemon's role and lease
    checks apply to the Release Manager rather than to the operator.
    """

    def __init__(self, session_id: str | None = None) -> None:
        self._session_id = session_id

    def get(self, path: str) -> tuple[int, object]:
        return self._send(path, "GET", None, None)

    def call_tool(self, server: str, tool: str, arguments: dict[str, object]) -> tuple[int, object]:
        headers = {"X-Gobby-CLI-Internal": "1"}
        if self._session_id is not None:
            headers["X-Gobby-Session-Id"] = self._session_id
        return self._send(f"/api/mcp/{server}/tools/{tool}", "POST", arguments, headers)

    def _send(
        self,
        path: str,
        method: str,
        body: dict[str, object] | None,
        headers: dict[str, str] | None,
    ) -> tuple[int, object]:
        from gobby.cli.utils_config import get_daemon_client
        from gobby.utils.daemon_client import DaemonAuthenticationError

        try:
            response = get_daemon_client(timeout=10).call_http_api(
                path, method=method, json_data=body, headers=headers
            )
        except (httpx.HTTPError, DaemonAuthenticationError):
            return 0, None
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None


def _announce(text: str) -> str | None:
    """Best-effort global notice; returns why it was not delivered, or None."""
    status, body = _DaemonProbe().call_tool(
        "gobby-agents", "send_message", {"target": "global", "content": text}
    )
    if status == 200 and isinstance(body, dict) and body.get("success") is True:
        return None
    return f"announcement not delivered (HTTP {status}): {text[:80]}"
