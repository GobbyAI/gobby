"""Opt-in host integration checks for Gobby's pinned SRT process wrapper."""

from __future__ import annotations

import asyncio
import json
import os
import pty
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.srt_runtime import SandboxLaunch, prepare_sandbox_launch
from gobby.cli.install_setup_srt import install_srt_runtime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform not in {"darwin", "linux"},
        reason="Gobby's SRT compatibility gate supports macOS and Linux",
    ),
]


def _prepare_launch(workspace: Path, run_id: str):
    install_srt_runtime()
    return asyncio.run(
        prepare_sandbox_launch(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            provider="codex",
            workspace_path=str(workspace),
            run_id=run_id,
            resolver=None,
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env=os.environ,
        )
    )


def _runner_argv(launch: SandboxLaunch, command: list[str]) -> list[str]:
    """Wrap a scripted child without the provider-executable argv[0] pin.

    ``SandboxLaunch.wrap`` pins argv[0] to the resolved provider executable
    (fa595efe8); these tests exercise runner mechanics with scripted children,
    so restore the scripted argv[0] after wrapping.
    """
    argv = launch.wrap(command)
    argv[argv.index("--") + 1] = command[0]
    return argv


def _wait_for(path: Path, text: str | None = None, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and (text is None or text in path.read_text(encoding="utf-8")):
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {path} to contain {text!r}")


def test_srt_allows_workspace_git_and_denies_sensitive_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gobby_home = Path(os.environ["GOBBY_HOME"])
    resolved_gobby_home = gobby_home.resolve()
    assert str(gobby_home).startswith("/var/")
    assert str(resolved_gobby_home).startswith("/private/var/")
    assert resolved_gobby_home == Path("/private") / gobby_home.relative_to("/")
    gobby_home.mkdir(parents=True, exist_ok=True)
    sensitive = gobby_home / "bootstrap.yaml"
    sensitive.write_text("must-not-leak", encoding="utf-8")
    escape_dir = gobby_home / "escape-target"
    escape_dir.mkdir()
    (workspace / "escape").symlink_to(escape_dir, target_is_directory=True)
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Gobby SRT Test"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "srt@gobby.local"],
        cwd=workspace,
        check=True,
    )
    launch = _prepare_launch(workspace, "host-filesystem")
    command = "; ".join(
        (
            "printf allowed > allowed.txt || exit 10",
            "git add allowed.txt || exit 11",
            "git commit -qm initial || exit 12",
            f"if cat {shlex.quote(str(sensitive))}; then exit 13; fi",
            "if printf escaped > escape/escaped.txt; then exit 14; fi",
            "exit 0",
        )
    )

    result = subprocess.run(
        _runner_argv(launch, ["/bin/sh", "-c", command]),
        cwd=workspace,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (workspace / "allowed.txt").read_text(encoding="utf-8") == "allowed"
    assert "must-not-leak" not in result.stdout
    assert not (escape_dir / "escaped.txt").exists()
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=workspace,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def test_srt_runner_preserves_tty_masks_credentials_and_forwards_terminal_signals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    credential = "fake-host-integration-token"
    monkeypatch.setenv("OPENAI_API_KEY", credential)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events = workspace / "events.jsonl"
    ready = workspace / "ready.json"
    script = workspace / "signals.mjs"
    script.write_text(
        """
import fs from "node:fs";
const append = (signal) => fs.appendFileSync("events.jsonl", JSON.stringify({signal}) + "\\n");
try {
  fs.writeFileSync(`${process.env.TMPDIR}/gobby-srt-temp`, "temp");
  fs.writeFileSync("ready.json", JSON.stringify({
    stdin: process.stdin.isTTY,
    stdout: process.stdout.isTTY,
    credential: process.env.OPENAI_API_KEY,
    tmpdir: process.env.TMPDIR,
    srtTmp: process.env.GOBBY_SRT_TMPDIR ?? null,
  }));
} catch (error) {
  fs.writeFileSync("startup-error.txt", error?.stack ?? String(error));
  process.exit(91);
}
for (const name of ["SIGWINCH", "SIGINT", "SIGHUP"]) process.on(name, () => append(name));
process.on("SIGTERM", () => { append("SIGTERM"); process.exit(0); });
setInterval(() => {}, 1000);
""".strip()
        + "\n",
        encoding="utf-8",
    )
    launch = _prepare_launch(workspace, "host-signals")
    master, slave = pty.openpty()
    process = subprocess.Popen(
        _runner_argv(launch, [launch.node_path or "node", str(script)]),
        cwd=workspace,
        env={**os.environ, **launch.provider_env},
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    try:
        try:
            _wait_for(ready, timeout=15)
        except pytest.fail.Exception:
            os.set_blocking(master, False)
            try:
                runner_output = os.read(master, 65_536).decode(errors="replace")
            except BlockingIOError:
                runner_output = ""
            violation_path = Path(launch.violation_path or "")
            violations = (
                violation_path.read_text(encoding="utf-8") if violation_path.is_file() else ""
            )
            startup_error_path = workspace / "startup-error.txt"
            startup_error = (
                startup_error_path.read_text(encoding="utf-8")
                if startup_error_path.is_file()
                else ""
            )
            pytest.fail(
                "SRT runner did not start; "
                f"exit={process.poll()}, output={runner_output!r}, "
                f"startup_error={startup_error!r}, violations={violations!r}",
                pytrace=False,
            )
        ready_payload = json.loads(ready.read_text(encoding="utf-8"))
        assert ready_payload["stdin"] is True
        assert ready_payload["stdout"] is True
        assert ready_payload["credential"] != credential
        # sandbox-runtime may pin the child TMPDIR itself (wrapped.env); the
        # runner-internal socket dir must never leak into the child either way.
        assert ready_payload["tmpdir"]
        assert ready_payload["tmpdir"] != launch.provider_env["GOBBY_SRT_TMPDIR"]
        assert ready_payload["srtTmp"] is None
        for name in ("SIGWINCH", "SIGINT", "SIGHUP", "SIGTERM"):
            os.kill(process.pid, getattr(signal, name))
            _wait_for(events, name)
        assert process.wait(timeout=15) == 0
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    observed = [
        json.loads(line)["signal"] for line in events.read_text(encoding="utf-8").splitlines()
    ]
    assert observed == ["SIGWINCH", "SIGINT", "SIGHUP", "SIGTERM"]
