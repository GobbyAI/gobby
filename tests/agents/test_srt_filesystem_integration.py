"""Host-level filesystem checks for the managed SRT boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest

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


def _run_srt(
    node: str, runner: Path, settings: Path, workspace: Path, script: str
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
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


@pytest.mark.parametrize(
    ("backend", "provider"),
    [("srt", None), ("provider-native", "claude")],
)
def test_supported_backend_blocks_sensitive_path_traversal_and_later_launch_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    backend: Literal["srt", "provider-native"],
    provider: Literal["claude"] | None,
) -> None:
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
    runtime = install_srt_runtime().path
    request.addfinalizer(lambda: _make_runtime_removable(runtime))
    runner = runtime / "runner.mjs"
    original_runner = runner.read_bytes()

    config = SandboxConfig(enabled=True, backend=backend, allow_network=False)
    paths = compute_sandbox_paths(
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
        assert _run_srt(node, runner, settings, workspace, command).returncode != 0

    assert sensitive.read_text(encoding="utf-8") == "operator-secret"
    assert runner.read_bytes() == original_runner
    later_launch = _run_srt(node, runner, settings, workspace, "exit 0")
    assert later_launch.returncode == 0, later_launch.stderr
