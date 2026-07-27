"""Focused tests for the managed Sandbox Runtime backend."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.agents import srt_runtime
from gobby.agents.sandbox import SandboxConfig, SandboxCredentialEnv, compute_sandbox_paths
from gobby.agents.sandbox_policy import _nearest_package_root
from gobby.agents.srt_runtime import (
    SRT_LOCKFILE_SHA256,
    SRT_NPM_INTEGRITY,
    SRT_PACKAGE,
    SRT_TARBALL_SHA256,
    SRT_TARBALL_URL,
    SRT_VERSION,
    SandboxLaunch,
    SrtInstallation,
    SrtRuntimeError,
    prepare_sandbox_launch,
    render_srt_settings,
    verify_srt_installation,
)


def test_render_settings_uses_srt_credential_schema() -> None:
    paths = SimpleNamespace(
        credential_env_vars=[
            SandboxCredentialEnv(
                name="OPENAI_API_KEY",
                mode="mask",
                inject_hosts=["api.openai.com"],
            )
        ],
        allowed_domains=["api.openai.com"],
        denied_domains=[],
        allow_unix_sockets=[],
        deny_read_paths=["/home/user/.ssh"],
        read_paths=["/workspace"],
        write_paths=["/workspace"],
        deny_write_paths=[],
    )

    settings = render_srt_settings(paths)

    assert settings["allowPty"] is True
    assert settings["network"]["strictAllowlist"] is True
    assert settings["network"]["tlsTerminate"] == {}
    assert settings["credentials"] == {
        "envVars": [
            {
                "name": "OPENAI_API_KEY",
                "mode": "mask",
                "injectHosts": ["api.openai.com"],
            }
        ],
        "allowPlaintextInject": False,
    }
    assert "inject_hosts" not in json.dumps(settings)


def test_package_root_discovery_preserves_worktree_carveout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    provider_bin = home / "bin"
    nested_package = home / "tools" / "droid"
    nested_bin = nested_package / "bin"
    workspace = home / ".gobby" / "worktrees" / "project"
    provider_bin.mkdir(parents=True)
    nested_bin.mkdir(parents=True)
    workspace.mkdir(parents=True)
    (home / "package.json").write_text("{}", encoding="utf-8")
    (nested_package / "package.json").write_text("{}", encoding="utf-8")
    home_executable = provider_bin / "droid"
    nested_executable = nested_bin / "droid"
    for executable in (home_executable, nested_executable):
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))

    assert _nearest_package_root(home_executable) is None
    assert _nearest_package_root(nested_executable) == nested_package

    paths = compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt", allow_network=False),
        str(workspace),
        provider="droid",
        env={"PATH": str(provider_bin)},
    )
    filesystem = render_srt_settings(paths)["filesystem"]

    assert str(home.resolve()) not in filesystem["allowRead"]
    assert str(workspace.resolve()) in filesystem["allowRead"]
    assert str((home / ".gobby").resolve()) in filesystem["denyRead"]


def test_compute_paths_masks_credentials_only_at_provider_api_hosts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = SandboxConfig(
        enabled=True,
        backend="srt",
        allow_network=False,
        allowed_domains=["telemetry.example"],
    )

    paths = compute_sandbox_paths(
        config,
        str(workspace),
        provider="codex",
        api_base="https://gateway.example/v1",
        env={"OPENAI_API_KEY": "secret", "PATH": os.environ.get("PATH", "")},
    )

    assert "gateway.example" in paths.allowed_domains
    assert "telemetry.example" in paths.allowed_domains
    assert paths.credential_env_vars == [
        SandboxCredentialEnv(
            name="OPENAI_API_KEY",
            mode="mask",
            inject_hosts=[
                "api.openai.com",
                "*.openai.com",
                "chatgpt.com",
                "*.chatgpt.com",
                "gateway.example",
            ],
        )
    ]


def test_git_and_package_network_are_separate_capabilities(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    default_paths = compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt", allow_network=False),
        str(workspace),
        provider="codex",
        env={"PATH": ""},
    )
    capable_paths = compute_sandbox_paths(
        SandboxConfig(
            enabled=True,
            backend="srt",
            allow_network=False,
            allow_git_network=True,
            allow_package_registries=True,
            denied_domains=["BLOCKED.EXAMPLE"],
        ),
        str(workspace),
        provider="codex",
        env={"PATH": ""},
    )

    assert "github.com" not in default_paths.allowed_domains
    assert "registry.npmjs.org" not in default_paths.allowed_domains
    assert "github.com" in capable_paths.allowed_domains
    assert "registry.npmjs.org" in capable_paths.allowed_domains
    assert "blocked.example" in capable_paths.denied_domains
    assert len(capable_paths.write_paths) > len(default_paths.write_paths)


@pytest.mark.asyncio
async def test_prepare_srt_launch_writes_private_policy_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    node = runtime / "node"
    runner = runtime / "runner.mjs"
    package_json = runtime / "package.json"
    for path in (node, runner, package_json):
        path.write_text("test", encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    monkeypatch.setattr(
        srt_runtime,
        "verify_srt_installation",
        lambda: SrtInstallation(runtime, node, runner, package_json),
    )
    preflights: list[tuple[SandboxLaunch, str, dict[str, str]]] = []

    async def fake_preflight(
        launch: SandboxLaunch,
        cwd: str,
        env: dict[str, str],
    ) -> None:
        preflights.append((launch, cwd, env))

    monkeypatch.setattr(srt_runtime, "_preflight_srt", fake_preflight)

    launch = await prepare_sandbox_launch(
        config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
        provider="codex",
        workspace_path=str(workspace),
        run_id="run/unsafe id",
        resolver=None,
        daemon_port=60887,
        websocket_port=60888,
        api_base=None,
        env={"PATH": os.environ.get("PATH", "")},
    )

    policy_path = Path(launch.policy_path or "")
    violation_path = Path(launch.violation_path or "")
    expected_parent = gobby_home / "run" / "sandbox" / "rununsafeid"
    assert launch.backend == "srt"
    assert launch.enforced is True
    assert launch.runtime_version == SRT_VERSION
    assert policy_path.parent == expected_parent
    assert violation_path.parent == expected_parent
    temp_path = expected_parent / "tmp"
    assert launch.provider_env == {"CLAUDE_CODE_TMPDIR": str(temp_path)}
    assert temp_path.stat().st_mode & 0o777 == 0o700
    assert workspace not in policy_path.parents
    assert policy_path.stat().st_mode & 0o777 == 0o600
    assert violation_path.stat().st_mode & 0o777 == 0o600
    assert expected_parent.stat().st_mode & 0o777 == 0o700
    assert preflights == [
        (
            launch,
            str(workspace),
            {
                "PATH": os.environ.get("PATH", ""),
                "CLAUDE_CODE_TMPDIR": str(temp_path),
            },
        )
    ]
    assert launch.wrap(["codex", "exec"]).count("codex") == 1
    assert launch.wrap(["codex", "exec"])[-2:] == ["codex", "exec"]


@pytest.mark.asyncio
async def test_prepare_srt_launch_rejects_unrestricted_network_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def unexpected_verify() -> Any:
        pytest.fail("the runtime must not be consulted for an invalid policy")

    monkeypatch.setattr(srt_runtime, "verify_srt_installation", unexpected_verify)

    with pytest.raises(SrtRuntimeError, match="does not accept unrestricted network access"):
        await prepare_sandbox_launch(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=True),
            provider="droid",
            workspace_path=str(workspace),
            run_id="run-network",
            resolver=None,
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env={},
        )


def test_verify_srt_installation_wraps_missing_lockfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    package_dir = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    package_dir.mkdir(parents=True)
    runner = root / "runner.mjs"
    bundled_runner = tmp_path / "srt_runner.mjs"
    runner.write_text("runner", encoding="utf-8")
    bundled_runner.write_text("runner", encoding="utf-8")
    (package_dir / "package.json").write_text(
        json.dumps({"name": SRT_PACKAGE, "version": SRT_VERSION}),
        encoding="utf-8",
    )
    (root / "receipt.json").write_text(
        json.dumps(
            {
                "package": SRT_PACKAGE,
                "version": SRT_VERSION,
                "tarball_url": SRT_TARBALL_URL,
                "tarball_sha256": SRT_TARBALL_SHA256,
                "npm_integrity": SRT_NPM_INTEGRITY,
                "lockfile_sha256": SRT_LOCKFILE_SHA256,
                "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(srt_runtime, "srt_install_root", lambda: root)
    monkeypatch.setattr(srt_runtime, "__file__", str(tmp_path / "srt_runtime.py"))

    with pytest.raises(SrtRuntimeError, match="missing or invalid"):
        verify_srt_installation()
