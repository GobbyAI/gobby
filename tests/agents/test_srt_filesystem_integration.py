"""Host-level filesystem checks for the managed SRT boundary."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

import pytest

from gobby.agents.claude_sandbox_home import prepare_claude_reviewer_home
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.agents.sandbox_resolvers import merge_claude_settings
from gobby.agents.srt_runtime import render_srt_settings
from gobby.cli.install_setup_srt import install_srt_runtime


def _make_runtime_removable(root: Path) -> None:
    """Restore owner write access so pytest can remove the immutable test runtime."""
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            continue
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "darwin", reason="SRT filesystem enforcement uses Seatbelt"),
]


def _nested_runner_socket_root() -> Path:
    """Check nested runner prerequisites and return its mux socket directory, or skip.

    sandbox-runtime allocates that socket under ``os.tmpdir()``, which the
    runner takes from GOBBY_SRT_TMPDIR. The directory cannot live inside
    ``tmp_path``: macOS limits ``sun_path`` to 104 bytes and pytest's temp
    directories exceed it. Managed executions permit sockets under their
    canonical current-run temp directory. Probe the bind rather than guessing
    from environment flags.
    """
    try:
        os.setpriority(os.PRIO_PROCESS, 0, os.getpriority(os.PRIO_PROCESS, 0))
    except OSError as exc:
        pytest.skip(f"host setpriority is unavailable for nested SRT in this sandbox: {exc}")
    root = Path(tempfile.gettempdir())
    probe_path = root / f"srt-probe-{os.getpid()}.sock"
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.bind(str(probe_path))
    except OSError as exc:
        pytest.skip(f"cannot bind a nested SRT runner socket under {root}: {exc}")
    finally:
        probe.close()
        probe_path.unlink(missing_ok=True)
    return root


def _run_srt(
    node: str,
    runner: Path,
    settings: Path,
    workspace: Path,
    script: str,
    socket_root: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            node,
            str(runner),
            "--settings",
            str(settings),
            "--violations",
            str(settings.with_name("violations.jsonl")),
            "--",
            "/bin/sh",
            "-c",
            script,
        ],
        cwd=workspace,
        env={**os.environ, "GOBBY_SRT_TMPDIR": str(socket_root)},
        capture_output=True,
        text=True,
        check=False,
        # A 20 s bound expired at load average ~27 under -n 4; this only catches hangs.
        timeout=120,
    )


@pytest.mark.parametrize(
    ("backend", "provider"),
    [("srt", None), ("provider-native", "claude")],
)
@pytest.mark.asyncio
async def test_supported_backend_blocks_sensitive_path_traversal_and_later_launch_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    backend: Literal["srt", "provider-native"],
    provider: Literal["claude"] | None,
) -> None:
    seatbelt = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1) (allow default)", "/usr/bin/true"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if seatbelt.returncode != 0 and "sandbox_apply: Operation not permitted" in seatbelt.stderr:
        pytest.skip("host-level SRT proof requires permission to apply a nested Seatbelt profile")
    assert seatbelt.returncode == 0, seatbelt.stderr

    gobby_home = tmp_path / "gobby-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sensitive = gobby_home / "bootstrap.yaml"
    sensitive.parent.mkdir(parents=True)
    sensitive.write_text("operator-secret", encoding="utf-8")
    linked = workspace / "bootstrap-link"
    linked.symlink_to(sensitive)
    replacement = workspace / "replacement"
    replacement.write_text("replacement", encoding="utf-8")

    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    socket_root = _nested_runner_socket_root()
    runtime = install_srt_runtime().path
    request.addfinalizer(lambda: _make_runtime_removable(runtime))
    runner = runtime / "runner.mjs"
    original_runner = runner.read_bytes()

    config = SandboxConfig(enabled=True, backend=backend, allow_network=False)
    paths = await compute_sandbox_paths(
        config,
        str(workspace),
        provider=provider,
        env={"PATH": ""},
    )
    payload = render_srt_settings(paths)
    if backend == "provider-native":
        native_filesystem = merge_claude_settings({}, config, paths)["sandbox"]["filesystem"]
        payload["filesystem"] = native_filesystem
    settings = workspace / "settings.json"
    settings.write_text(json.dumps(payload), encoding="utf-8")
    settings.with_name("violations.jsonl").write_text("", encoding="utf-8")

    denied_commands = (
        f"cat {sensitive}",
        f"cat {workspace / '..' / 'gobby-home' / 'bootstrap.yaml'}",
        f"cat {linked}",
        f"printf hacked > {sensitive}",
        f"mv {replacement} {sensitive}",
        f"printf hacked >> {runner}",
    )
    for command in denied_commands:
        denied = _run_srt(node, runner, settings, workspace, command, socket_root)
        assert denied.returncode != 0

    assert sensitive.read_text(encoding="utf-8") == "operator-secret"
    assert runner.read_bytes() == original_runner
    later_launch = _run_srt(node, runner, settings, workspace, "exit 0", socket_root)
    assert later_launch.returncode == 0, later_launch.stderr


@pytest.mark.asyncio
async def test_claude_reviewer_private_controls_deny_replacement_and_keep_runtime_writable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Exercise installed SRT against dummy settings and current-workspace transcripts."""
    home = tmp_path / "home"
    host = home / ".claude"
    host.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings_source = host / "settings.json"
    settings_source.write_text('{"env": {"DUMMY_TOKEN": "fixture-only"}}', encoding="utf-8")
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"gobby": {"command": "gobby"}}}), encoding="utf-8"
    )
    cache = tmp_path / "own-run-cache"
    private_home = prepare_claude_reviewer_home(
        cache, {}, workspace=str(workspace), auto_approve=True
    )
    private = Path(private_home.environment["CLAUDE_CONFIG_DIR"])
    paths = await compute_sandbox_paths(
        SandboxConfig(
            enabled=True,
            backend="srt",
            allow_network=False,
            extra_write_paths=[str(cache)],
            extra_deny_write_paths=list(private_home.protected_write_paths),
        ),
        str(workspace),
        provider="claude",
        env={"PATH": ""},
    )
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    socket_root = _nested_runner_socket_root()
    runtime = install_srt_runtime().path
    request.addfinalizer(lambda: _make_runtime_removable(runtime))
    policy = workspace / "settings.json"
    policy.write_text(json.dumps(render_srt_settings(paths)), encoding="utf-8")
    violations = policy.with_name("violations.jsonl")
    violations.touch()
    runner = runtime / "runner.mjs"
    denied_commands = [
        ("host-settings", f"printf hacked > {shlex.quote(str(settings_source))}"),
        ("private-settings", f"printf hacked > {shlex.quote(str(private / 'settings.json'))}"),
        (
            "replace-settings",
            f"rm -f {shlex.quote(str(private / 'settings.json'))} && "
            f"ln -s {shlex.quote(str(settings_source))} {shlex.quote(str(private / 'settings.json'))}",
        ),
    ]
    for control in ("agents", "skills", "commands", "plugins/cache", "plugins/synced"):
        denied_commands.append((control, f"mkdir -p {shlex.quote(str(private / control))}"))
    for label, command in denied_commands:
        result = _run_srt(node, runner, policy, workspace, command, socket_root)
        print(f"EXPECTED_DENIAL {label} exit={result.returncode}")
        assert result.returncode != 0, (label, result.stdout, result.stderr)
    assert settings_source.read_text(encoding="utf-8") == '{"env": {"DUMMY_TOKEN": "fixture-only"}}'
    assert (private / "settings.json").read_bytes() == settings_source.read_bytes()
    state = Path(private_home.environment["XDG_STATE_HOME"]) / "claude" / "locks"
    transcript = next((private / "projects").iterdir()) / "review.jsonl"
    command = (
        f"mkdir -p {shlex.quote(str(state))} && "
        f"printf updated > {shlex.quote(str(private / '.claude.json'))} && "
        f"touch {shlex.quote(str(state / '2.1.296.lock'))} && "
        f"printf evidence > {shlex.quote(str(transcript))}"
    )
    result = _run_srt(node, runner, policy, workspace, command, socket_root)
    print(f"MUTABLE_STATE exit={result.returncode} PROOF={policy} VIOLATIONS={violations}")
    assert result.returncode == 0, result.stderr
    assert (state / "2.1.296.lock").is_file()
    assert (private / ".claude.json").read_text(encoding="utf-8") == "updated"
    assert transcript.resolve().read_text(encoding="utf-8") == "evidence"
