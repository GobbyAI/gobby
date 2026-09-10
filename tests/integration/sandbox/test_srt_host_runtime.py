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
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_reaper import reap_sandbox_run_roots
from gobby.agents.srt_runtime import SandboxLaunch, prepare_sandbox_launch
from gobby.cli.install_setup_srt import install_srt_runtime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform not in {"darwin", "linux"},
        reason="Gobby's SRT compatibility gate supports macOS and Linux",
    ),
]


def _prepare_launch(workspace: Path, run_id: str) -> SandboxLaunch:
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


def test_srt_managed_run_socket_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise real OS policy: run-local sockets on macOS, none on Linux/WSL."""
    # Keep sockaddr_un paths short without assuming /tmp is writable in a parent sandbox.
    with tempfile.TemporaryDirectory(prefix="") as temporary, ExitStack() as cleanup:
        root = Path(temporary).resolve()
        monkeypatch.setenv("GOBBY_HOME", str(root))
        cleanup.callback(lambda: asyncio.run(reap_sandbox_run_roots("a", gobby_home=root)))
        # The policy probe executes Python, so provider executable discovery
        # should not require a separately installed/authenticated Codex CLI.
        provider_bin = root / "bin"
        provider_bin.mkdir()
        (provider_bin / "codex").symlink_to(sys.executable)
        monkeypatch.setenv("PATH", f"{provider_bin}{os.pathsep}{os.environ['PATH']}")
        workspace = root / "w"
        workspace.mkdir()
        other_run = root / "run" / "sandbox" / "b" / "tmp"
        other_run.mkdir(parents=True)
        install_srt_runtime()
        launch = asyncio.run(
            prepare_sandbox_launch(
                config=SandboxConfig(
                    enabled=True,
                    backend="srt",
                    allow_network=False,
                    extra_write_paths=[str(other_run)],
                ),
                provider="codex",
                workspace_path=str(workspace),
                run_id="a",
                resolver=None,
                daemon_port=60887,
                websocket_port=60888,
                api_base=None,
                env=os.environ,
                allow_run_unix_sockets=True,
            )
        )
        current_tmp = Path(launch.provider_env["TMPDIR"])
        (current_tmp / "escape").symlink_to(workspace, target_is_directory=True)
        paths = [
            current_tmp / "n" / "s",
            other_run / "s",
            workspace / "s",
            current_tmp / "escape" / "s",
        ]
        assert max(len(os.fsencode(path)) for path in paths) < 104
        script = workspace / "probe.py"
        script.write_text(
            """import errno, json, os, socket, sys
from pathlib import Path
root, other, outside, escape = map(Path, sys.argv[1:])
assert Path(os.environ['TMPDIR']) == root, (os.environ['TMPDIR'], str(root))
(root / 'n' / 'nested').mkdir(parents=True)
(root / 'n' / 'nested' / 'written').write_text('ok')
(other / 'written').write_text('ok')
(outside / 'written').write_text('ok')
results = {}
for label, path in [('current', root / 'n' / 's'), ('other_run', other / 's'),
                    ('outside', outside / 's'), ('symlink_escape', escape / 's')]:
    try:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            listener.listen(1)
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(2)
                client.connect(str(path))
                connection, _ = listener.accept()
                with connection:
                    client.sendall(b'ping')
                    assert connection.recv(4) == b'ping'
            results[label] = 'allowed'
    except OSError as error:
        assert error.errno in (errno.EPERM, errno.EACCES), (label, error)
        results[label] = 'denied'
    finally:
        path.unlink(missing_ok=True)
print(json.dumps(results))
""",
            encoding="utf-8",
        )
        result = subprocess.run(
            _runner_argv(
                launch,
                [
                    sys.executable,
                    str(script),
                    str(current_tmp),
                    str(other_run),
                    str(workspace),
                    str(current_tmp / "escape"),
                ],
            ),
            cwd=workspace,
            env={**os.environ, **launch.provider_env},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "current": "allowed" if sys.platform == "darwin" else "denied",
            "other_run": "denied",
            "outside": "denied",
            "symlink_escape": "denied",
        }
        assert (current_tmp / "n" / "nested" / "written").read_text() == "ok"


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


def test_srt_login_zsh_heredoc_uses_run_temp(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launch = _prepare_launch(workspace, "host-zsh-heredoc")
    _, environment = launch.compose_subprocess([], os.environ)
    result = subprocess.run(
        _runner_argv(launch, ["/bin/zsh", "-lc", "cat <<'EOF'\nheredoc-ok\nEOF"]),
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "heredoc-ok\n"


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
  fs.writeFileSync("ready.json", JSON.stringify({
    stdin: process.stdin.isTTY,
    stdout: process.stdout.isTTY,
    credential: process.env.OPENAI_API_KEY,
    tmpdir: process.env.TMPDIR,
    claudeTmpdir: process.env.CLAUDE_CODE_TMPDIR ?? null,
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
        assert ready_payload["tmpdir"] == launch.provider_env["TMPDIR"]
        assert ready_payload["claudeTmpdir"] == os.environ.get("CLAUDE_CODE_TMPDIR")
        assert ready_payload["tmpdir"] != launch.provider_env["GOBBY_SRT_TMPDIR"]
        assert ready_payload["srtTmp"] is None
        for name in ("SIGWINCH", "SIGINT", "SIGHUP", "SIGTERM"):
            os.kill(process.pid, getattr(signal, name))
            _wait_for(events, name)
        assert process.wait(timeout=15) == 0
    finally:
        os.close(master)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    observed = [
        json.loads(line)["signal"] for line in events.read_text(encoding="utf-8").splitlines()
    ]
    assert observed == ["SIGWINCH", "SIGINT", "SIGHUP", "SIGTERM"]
