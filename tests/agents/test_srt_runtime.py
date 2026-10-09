"""Focused tests for the managed Sandbox Runtime backend."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import threading
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.agents import srt_runtime
from gobby.agents.sandbox import (
    ResolvedSandboxPaths,
    SandboxConfig,
    SandboxCredentialEnv,
    compute_sandbox_paths,
)
from gobby.agents.sandbox_policy import _nearest_package_root, previous_run_write_paths
from gobby.agents.sandbox_resolvers import (
    ClaudeSandboxResolver,
    CodexSandboxResolver,
    GrokSandboxResolver,
    SandboxResolver,
)
from gobby.agents.srt_runtime import (
    SandboxLaunch,
    SrtInstallation,
    SrtRuntimeError,
    prepare_sandbox_launch,
    render_srt_settings,
    verify_srt_installation,
)
from gobby.utils import spawn
from gobby.utils.dependency_requirements import (
    SRT_RELEASE,
    DependencyStatus,
)

pytestmark = pytest.mark.unit


def test_previous_run_write_paths_keeps_shared_hook_inbox_writable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    superseded = previous_run_write_paths({})

    assert str((gobby_home / "hooks" / "inbox").resolve()) not in superseded


def test_render_settings_uses_srt_credential_schema() -> None:
    paths = ResolvedSandboxPaths(
        workspace_path="/workspace",
        read_paths=["/workspace"],
        write_paths=["/workspace"],
        allow_external_network=False,
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


def test_render_settings_ignores_only_benign_violation_noise() -> None:
    paths = ResolvedSandboxPaths(
        workspace_path="/workspace",
        read_paths=["/workspace"],
        write_paths=["/workspace"],
        allow_external_network=False,
        credential_env_vars=[],
        allowed_domains=[],
        denied_domains=[],
        allow_unix_sockets=[],
        deny_read_paths=[],
        deny_write_paths=[],
    )

    ignored = render_srt_settings(paths)["ignoreViolations"]

    assert ignored == {
        "*": [
            "sysctl-read kern.iossupportversion",
            "system-info vfs.disk-space",
            "mach-lookup com.apple.SystemConfiguration.configd",
            "system-info net.link.addr",
            "mach-lookup com.apple.FSEvents",
            "mach-lookup com.apple.DiskArbitration.diskarbitrationd",
            "sysctl-read hw.optional.",
            "sysctl-read hw.cpusubfamily",
        ]
    }
    # SRT drops a violation when its log line contains any "*" pattern
    # (sandbox-violation-store.js shouldIgnoreViolation).
    noise = [
        "bash(3482) deny(1) sysctl-read kern.iossupportversion",
        "rustc(812) deny(1) system-info vfs.disk-space",
        "codex(77) deny(1) mach-lookup com.apple.SystemConfiguration.configd",
        "gcode(55) deny(1) system-info net.link.addr",
        "node(60) deny(1) mach-lookup com.apple.FSEvents",
        "python3.14(91) deny(1) mach-lookup com.apple.DiskArbitration.diskarbitrationd",
        "node(60) deny(1) sysctl-read hw.optional.arm.FEAT_SHA3",
        "codex(77) deny(1) sysctl-read hw.cpusubfamily",
    ]
    true_positives = [
        "python3.14(91) deny(1) file-read-metadata /Users/x/.gobby/bootstrap.yaml",
        "python3.14(91) deny(1) file-write-mode /Users/x/.gobby/machine_id",
        "codex(77) deny(1) network-outbound 203.0.113.7:443",
    ]
    assert all(any(p in line for p in ignored["*"]) for line in noise)
    assert not any(any(p in line for p in ignored["*"]) for line in true_positives)
    # The failed-DNS fallback carries no target; it must stay recorded.
    assert not any("network-outbound" in p for p in ignored["*"])


async def test_package_root_discovery_preserves_worktree_carveout(
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

    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt", allow_network=False),
        str(workspace),
        provider="droid",
        env={"PATH": str(provider_bin)},
    )
    filesystem = render_srt_settings(paths)["filesystem"]

    assert str(home.resolve()) not in filesystem["allowRead"]
    assert str(workspace.resolve()) in filesystem["allowRead"]
    assert str((home / ".gobby" / "bootstrap.yaml").resolve()) in filesystem["denyRead"]
    assert str((home / ".gobby").resolve()) not in filesystem["denyRead"]


async def test_provider_state_roots_are_writable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))

    for provider, state_root in (("codex", ".codex"), ("droid", ".factory")):
        paths = await compute_sandbox_paths(
            SandboxConfig(enabled=True, backend="srt", allow_network=False),
            str(workspace),
            provider=provider,
            env={"PATH": ""},
        )
        filesystem = render_srt_settings(paths)["filesystem"]
        state_path = str((home / state_root).resolve())

        assert state_path in filesystem["allowWrite"]
        assert state_path in filesystem["allowRead"]
        assert str((home / ".ssh").resolve()) in paths.deny_write_paths

        gobby_home = (home / ".gobby").resolve()
        assert str(gobby_home) not in filesystem["allowRead"]
        assert str(gobby_home) not in filesystem["allowWrite"]
        assert str(gobby_home / "bootstrap.yaml") in filesystem["denyRead"]

        uv_root = str((home / ".local" / "share" / "uv").resolve())
        assert uv_root in filesystem["allowRead"]
        assert uv_root not in filesystem["allowWrite"]


async def test_rendered_write_denies_are_the_ones_a_grant_can_reach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
    srt_debug_deny = home / ".claude" / "debug" / "synthetic"

    paths = await compute_sandbox_paths(
        SandboxConfig(
            enabled=True,
            backend="srt",
            allow_network=False,
            extra_deny_write_paths=[str(srt_debug_deny)],
        ),
        str(workspace),
        provider="grok",
        env={"PATH": ""},
    )
    deny_write = render_srt_settings(paths)["filesystem"]["denyWrite"]

    root = workspace.resolve()
    assert str(root / ".codex" / "hooks.json") in deny_write
    assert str(root / ".grok" / "hooks") in deny_write
    # SRT always grants its own debug directory, so a deny inside it still counts.
    assert str(srt_debug_deny) in deny_write
    # Nothing grants the workspace's parent; default-deny already covers its controls.
    parent_hooks = str(tmp_path / ".codex" / "hooks.json")
    assert parent_hooks in paths.deny_write_paths
    assert parent_hooks not in deny_write
    # Each deny costs Seatbelt compile time, superlinearly: 1978 took 13.7 s of CPU.
    # This workspace renders 55.
    assert len(deny_write) < 100 < len(paths.deny_write_paths)


def test_rendering_keeps_write_denies_under_each_pinned_default_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Independently captured from getDefaultWritePaths() in verified SRT 0.0.79.
    # A pin bump requires refreshing this contract, without a network fetch in pytest.
    assert SRT_RELEASE.version == "0.0.79"
    defaults = (
        "/dev/stdout",
        "/dev/stderr",
        "/dev/null",
        "/dev/tty",
        "/dev/dtracehelper",
        "/dev/autofs_nowait",
        "/tmp/claude",
        "/private/tmp/claude",
        "~/.npm/_logs",
        "~/.claude/debug",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    denies = [str(Path(path).expanduser() / "synthetic") for path in defaults]
    paths = ResolvedSandboxPaths(
        workspace_path=str(tmp_path),
        read_paths=[],
        write_paths=[],
        allow_external_network=False,
        deny_write_paths=denies,
    )

    assert render_srt_settings(paths)["filesystem"]["denyWrite"] == denies


def test_write_deny_under_a_symlinked_srt_default_is_rendered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    dotfiles = tmp_path / "dotfiles"
    home.mkdir()
    dotfiles.mkdir()
    (home / ".claude").symlink_to(dotfiles, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    # SRT grants ~/.claude/debug by its realpath, so a deny spelled through the target counts.
    deny = str(dotfiles.resolve() / "debug" / "synthetic")
    paths = ResolvedSandboxPaths(
        workspace_path=str(tmp_path),
        read_paths=[],
        write_paths=[],
        allow_external_network=False,
        deny_write_paths=[deny],
    )

    assert render_srt_settings(paths)["filesystem"]["denyWrite"] == [deny]


async def test_claude_account_auth_files_are_read_only_sandbox_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))

    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt", allow_network=False),
        str(workspace),
        provider="claude",
        env={"PATH": ""},
    )
    filesystem = render_srt_settings(paths)["filesystem"]
    claude_config = str((home / ".claude.json").resolve())
    login_keychain = str((home / "Library" / "Keychains" / "login.keychain-db").resolve())

    assert claude_config in filesystem["allowRead"]
    assert login_keychain in filesystem["allowRead"]
    assert claude_config not in filesystem["allowWrite"]
    assert login_keychain not in filesystem["allowWrite"]


async def test_droid_reads_only_the_login_keychain_among_credential_stores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Droid's login is unreadable without its login keychain item (#23554 A/B/C/D runs).
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))

    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt", allow_network=False),
        str(workspace),
        provider="droid",
        env={"PATH": ""},
    )
    filesystem = render_srt_settings(paths)["filesystem"]
    keychains = (home / "Library" / "Keychains").resolve()
    login_keychain = str(keychains / "login.keychain-db")

    assert login_keychain in filesystem["allowRead"]
    assert login_keychain not in filesystem["allowWrite"]
    assert str(keychains / "other.keychain-db") not in filesystem["allowRead"]
    assert str((home / ".claude.json").resolve()) not in filesystem["allowRead"]
    for store in (keychains, home / ".ssh", home / ".aws", home / ".config" / "gh"):
        assert str(store.resolve()) in filesystem["denyRead"]
        assert str(store.resolve()) not in filesystem["allowRead"]


async def test_droid_reaches_its_auth_host_without_receiving_injected_credentials(
    tmp_path: Path,
) -> None:
    # Droid refreshes an expired login through WorkOS; FACTORY_API_KEY stays Factory-only.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = SandboxConfig(enabled=True, backend="srt", allow_network=False)
    env = {"FACTORY_API_KEY": "secret", "PATH": os.environ.get("PATH", "")}

    droid = await compute_sandbox_paths(config, str(workspace), provider="droid", env=env)
    codex = await compute_sandbox_paths(config, str(workspace), provider="codex", env=env)

    assert "api.workos.com" in droid.allowed_domains
    assert "*.workos.com" not in droid.allowed_domains
    assert "api.workos.com" not in codex.allowed_domains
    assert droid.credential_env_vars == [
        SandboxCredentialEnv(
            name="FACTORY_API_KEY",
            mode="mask",
            inject_hosts=["api.factory.ai", "*.factory.ai"],
        )
    ]


async def test_compute_paths_masks_credentials_only_at_provider_api_hosts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = SandboxConfig(
        enabled=True,
        backend="srt",
        allow_network=False,
        allowed_domains=["telemetry.example"],
    )

    paths = await compute_sandbox_paths(
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


async def test_git_and_package_network_are_separate_capabilities(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    default_paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt", allow_network=False),
        str(workspace),
        provider="codex",
        env={"PATH": ""},
    )
    capable_paths = await compute_sandbox_paths(
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
    # Both flags are network capabilities only. Local toolchain caches are
    # writable either way: an offline `cargo build` still takes the
    # $CARGO_HOME/.package-cache lock (#19443).
    assert set(capable_paths.write_paths) == set(default_paths.write_paths)


async def test_network_capabilities_are_preserved_without_a_provider(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    paths = await compute_sandbox_paths(
        SandboxConfig(
            enabled=True,
            backend="srt",
            allow_network=False,
            allowed_domains=["operator.example"],
            denied_domains=["BLOCKED.EXAMPLE"],
            allow_git_network=True,
            allow_package_registries=True,
        ),
        str(workspace),
        provider=None,
        env={"PATH": ""},
    )

    assert "operator.example" in paths.allowed_domains
    assert "github.com" in paths.allowed_domains
    assert "registry.npmjs.org" in paths.allowed_domains
    assert paths.denied_domains == ["blocked.example"]
    assert paths.loopback_ports == [60887, 60888]


@pytest.mark.parametrize(
    ("provider", "temp_env_name", "platform_name"),
    [
        ("claude", "CLAUDE_CODE_TMPDIR", "darwin"),
        ("claude", "CLAUDE_CODE_TMPDIR", "linux"),
        ("codex", "TMPDIR", "darwin"),
        ("codex", "TMPDIR", "linux"),
        ("grok", "TMPDIR", "darwin"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.parametrize("allow_run_sockets", [False, True])
async def test_prepare_srt_launch_writes_private_policy_and_keeps_ghook_inbox_writable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
    temp_env_name: str,
    platform_name: str,
    allow_run_sockets: bool,
) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(srt_runtime, "sys", SimpleNamespace(platform=platform_name))
    gobby_home = tmp_path / "gobby-home"
    workspace = tmp_path / "workspace"
    untrusted_mcp_root = tmp_path / "untrusted-mcp-root"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    untrusted_mcp_root.mkdir()
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "untrusted": {
                        "command": "node",
                        "args": [str(untrusted_mcp_root)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    runtime.mkdir()
    node = runtime / "node"
    runner = runtime / "runner.mjs"
    package_json = runtime / "package.json"
    for path in (node, runner, package_json):
        path.write_text("test", encoding="utf-8")
    provider_root = tmp_path / provider
    provider_target = provider_root / "versions" / "2.1.220"
    provider_target.parent.mkdir(parents=True)
    provider_target.write_text("#!/bin/sh\n", encoding="utf-8")
    provider_target.chmod(0o755)
    (provider_root / "package.json").write_text("{}", encoding="utf-8")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    provider_shim = shim_dir / provider
    provider_shim.symlink_to(provider_target)
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    hook_inbox = gobby_home / "hooks" / "inbox"
    hook_inbox.mkdir(parents=True)

    def verified_installation(**_context: str | None) -> SrtInstallation:
        return SrtInstallation(runtime, node, runner, package_json)

    monkeypatch.setattr(srt_runtime, "verify_srt_installation", verified_installation)
    preflights: list[tuple[SandboxLaunch, str, dict[str, str]]] = []

    async def fake_preflight(
        launch: SandboxLaunch,
        cwd: str,
        env: dict[str, str],
    ) -> None:
        preflights.append((launch, cwd, env))

    monkeypatch.setattr(srt_runtime, "_preflight_srt", fake_preflight)
    original_which = shutil.which
    provider_lookups = 0

    def fake_which(
        command: str,
        mode: int = os.F_OK | os.X_OK,
        path: str | None = None,
    ) -> str | None:
        nonlocal provider_lookups
        if command == provider:
            provider_lookups += 1
            return str(provider_shim)
        result = original_which(command, mode=mode, path=path)
        return None if result is None else str(result)

    monkeypatch.setattr(shutil, "which", fake_which)
    # A spawn hands the run the unsandboxed Cargo home and checkout target with grants.
    unsandboxed_cargo = {
        "CARGO_HOME": gobby_home / "cache" / "cargo-home",
        "CARGO_TARGET_DIR": gobby_home / "cache" / "cargo-target-v2" / "project" / "workspace",
    }
    spawn_env = {"PATH": str(shim_dir)} | {
        name: str(path) for name, path in unsandboxed_cargo.items()
    }

    launch = await prepare_sandbox_launch(
        config=SandboxConfig(
            enabled=True,
            backend="srt",
            allow_network=False,
            extra_write_paths=[str(hook_inbox), *map(str, unsandboxed_cargo.values())],
            allow_unix_sockets=[str(workspace / "operator.sock")],
        ),
        provider=provider,
        workspace_path=str(workspace),
        run_id="run/unsafe id",
        resolver=None,
        daemon_port=60887,
        websocket_port=60888,
        api_base=None,
        env=spawn_env,
        allow_run_unix_sockets=allow_run_sockets,
    )

    policy_path = Path(launch.policy_path or "")
    violation_path = Path(launch.violation_path or "")
    expected_parent = gobby_home / "run" / "sandbox" / "rununsafeid"
    assert launch.backend == "srt"
    assert launch.enforced is True
    assert launch.provider_executable == str(provider_target.resolve())
    assert launch.runtime_version == SRT_RELEASE.version
    assert policy_path.parent == expected_parent / "assets"
    assert violation_path.parent == expected_parent / "logs"
    temp_path = Path(launch.provider_env[temp_env_name])
    if allow_run_sockets and platform_name == "darwin":
        assert temp_path.parent == tmp_path.resolve()
        assert (expected_parent / "tmp-path").read_text() == str(temp_path)
    else:
        assert temp_path == expected_parent / "tmp"
    # Children the provider spawns (the uv-launched MCP bridge) read TMPDIR, so
    # every provider gets it pointed at the same writable run temp directory.
    assert Path(launch.provider_env["TMPDIR"]) == temp_path
    assert "GOBBY_HOOK_SPOOL" not in launch.provider_env
    mux_dir = gobby_home / "runtime" / "srt-sock"
    assert launch.provider_env["GOBBY_SRT_TMPDIR"] == str(mux_dir)
    assert mux_dir.is_dir()
    assert mux_dir.stat().st_mode & 0o777 == 0o700
    assert Path(launch.provider_env["UV_CACHE_DIR"]).is_relative_to(expected_parent / "cache")
    sandbox_cache = gobby_home / "cache" / "sandbox"
    for name in unsandboxed_cargo:
        assert Path(launch.provider_env[name]).is_relative_to(sandbox_cache)
    assert not (expected_parent / "cache" / "cargo-home").exists()
    for writable_name in ("hooks", "logs", "cache"):
        writable = expected_parent / writable_name
        assert writable.is_dir()
        assert writable.stat().st_mode & 0o777 == 0o700
    assert temp_path.stat().st_mode & 0o777 == 0o700
    assert workspace not in policy_path.parents
    assert policy_path.stat().st_mode & 0o777 == 0o600
    assert violation_path.stat().st_mode & 0o777 == 0o600
    assert expected_parent.stat().st_mode & 0o777 == 0o700
    assert len(preflights) == 1
    preflight_launch, preflight_cwd, preflight_env = preflights[0]
    assert preflight_launch is launch
    assert preflight_cwd == str(workspace)
    assert preflight_env == {**spawn_env, **launch.provider_env}
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    socket_grants = policy["network"]["allowUnixSockets"]
    assert str((workspace / "operator.sock").resolve()) in socket_grants
    assert (str(temp_path.resolve()) in socket_grants) == (
        allow_run_sockets and platform_name == "darwin"
    )
    assert policy["network"]["allowAllUnixSockets"] is False
    assert str(temp_path.resolve()) in policy["filesystem"]["allowWrite"]
    allowed_reads = policy["filesystem"]["allowRead"]
    allowed_writes = policy["filesystem"]["allowWrite"]
    assert str(hook_inbox.resolve()) in allowed_writes
    for name, unsandboxed_path in unsandboxed_cargo.items():
        assert allowed_writes.count(str(Path(launch.provider_env[name]).resolve())) == 1
        assert str(unsandboxed_path.resolve()) not in allowed_writes
    # The daemon rebuilds the Zig mirror below this root before each run, so no run
    # may plant links there for the next one to follow.
    assert str(sandbox_cache.resolve()) not in allowed_writes
    assert str(provider_target.resolve()) in allowed_reads
    assert str(provider_root.resolve()) in allowed_reads
    assert str(untrusted_mcp_root.resolve()) not in allowed_reads
    assert str(provider_shim.absolute()) not in allowed_reads
    assert str(shim_dir.resolve()) not in allowed_reads
    wrapped = launch.wrap([provider, "--version"])
    assert wrapped[wrapped.index("--") + 1] == str(provider_target.resolve())
    assert launch.metadata()["provider_executable"] == str(provider_target.resolve())
    assert provider_lookups == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "resolver"),
    [
        ("codex", CodexSandboxResolver()),
        ("droid", None),
        ("grok", GrokSandboxResolver()),
    ],
)
async def test_provider_native_launch_rejects_unproven_sensitive_path_enforcement(
    tmp_path: Path,
    provider: str,
    resolver: SandboxResolver | None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(SrtRuntimeError, match="sensitive-root contract"):
        await prepare_sandbox_launch(
            config=SandboxConfig(enabled=True, backend="provider-native", allow_network=False),
            provider=provider,
            workspace_path=str(workspace),
            run_id="run-1",
            resolver=resolver,
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env={"PATH": ""},
        )


@pytest.mark.asyncio
async def test_claude_provider_native_preflight_emits_sensitive_denies(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    gobby_home = tmp_path / "gobby-home"
    workspace.mkdir()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        launch = await prepare_sandbox_launch(
            config=SandboxConfig(enabled=True, backend="provider-native", allow_network=False),
            provider="claude",
            workspace_path=str(workspace),
            run_id="run-1",
            resolver=ClaudeSandboxResolver(),
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env={"PATH": ""},
        )

    settings = json.loads(launch.provider_args[1])
    filesystem = settings["sandbox"]["filesystem"]
    assert str((gobby_home / "bootstrap.yaml").resolve()) in filesystem["denyRead"]
    assert str((gobby_home / "tools" / "srt").resolve()) in filesystem["denyWrite"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolution", "expected_match"),
    [
        ("missing", "claude executable"),
        ("broken", "Failed to resolve"),
    ],
)
async def test_prepare_srt_launch_fails_before_policy_for_unresolved_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resolution: str,
    expected_match: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broken = tmp_path / "bin" / "claude"
    broken.parent.mkdir()
    broken.symlink_to(tmp_path / "missing-target")
    resolved = None if resolution == "missing" else str(broken)
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    def unexpected_verify() -> Any:
        pytest.fail("runtime verification must not run before provider resolution")

    monkeypatch.setattr(
        shutil,
        "which",
        lambda command, mode=os.F_OK | os.X_OK, path=None: (
            resolved if command == "claude" else None
        ),
    )
    monkeypatch.setattr(srt_runtime, "verify_srt_installation", unexpected_verify)

    with pytest.raises(SrtRuntimeError, match=expected_match):
        await prepare_sandbox_launch(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            provider="claude",
            workspace_path=str(workspace),
            run_id=f"run-{resolution}",
            resolver=None,
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env={"PATH": str(broken.parent)},
        )

    assert not (gobby_home / "run" / "sandbox").exists()


@pytest.mark.parametrize(
    "launch",
    [
        SandboxLaunch(
            backend="provider-native",
            enforced=True,
            provider_executable="/resolved/claude",
        ),
        SandboxLaunch(
            backend="srt",
            enforced=False,
            provider_executable="/resolved/claude",
        ),
    ],
)
def test_non_enforced_srt_and_provider_native_launches_keep_provider_argv(
    launch: SandboxLaunch,
) -> None:
    command = ["claude", "--version"]

    assert launch.wrap(command) == command


def _enforced_srt_launch() -> SandboxLaunch:
    return SandboxLaunch(
        backend="srt",
        enforced=True,
        node_path="/usr/bin/node",
        runner_path="/opt/srt/runner.js",
        policy_path="/tmp/srt-policy.json",
        violation_path="/tmp/srt-violations.jsonl",
        provider_executable="/resolved/claude",
        provider_env={"TMPDIR": "/tmp/srt-run", "GOBBY_SRT_TMPDIR": "/tmp/srt-sock"},
    )


def test_emit_cli_shim_execs_wrapped_argv_and_passes_sdk_args(tmp_path: Path) -> None:
    launch = _enforced_srt_launch()
    shim = launch.emit_cli_shim(command=["claude"], directory=tmp_path)

    assert shim.is_file()
    assert shim.stat().st_mode & 0o111
    body = shim.read_text(encoding="utf-8")
    wrapped = launch.wrap(["claude"])
    for token in wrapped:
        assert token in body
    assert '"$@"' in body
    assert body.count("exec") == 1
    launch.cleanup_cli_shim()
    assert not shim.exists()


def test_compose_sandbox_subprocess_merges_identity_then_provider_env() -> None:
    launch = _enforced_srt_launch()
    identity = {"PATH": "/bin", "GOBBY_SESSION_ID": "sess-1", "TMPDIR": "/tmp/identity"}
    argv, env = launch.compose_subprocess(["claude", "--print"], identity)

    assert argv == launch.wrap(["claude", "--print"])
    assert env["GOBBY_SESSION_ID"] == "sess-1"
    assert env["PATH"] == "/bin"
    assert env["TMPDIR"] == "/tmp/srt-run"
    assert env["GOBBY_SRT_TMPDIR"] == "/tmp/srt-sock"


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "runtime"
    package_dir = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    package_dir.mkdir(parents=True)
    runner = root / "runner.mjs"
    bundled_runner = tmp_path / "srt_runner.mjs"
    runner.write_text("runner", encoding="utf-8")
    bundled_runner.write_text("runner", encoding="utf-8")
    (package_dir / "package.json").write_text(
        json.dumps({"name": SRT_RELEASE.package, "version": SRT_RELEASE.version}),
        encoding="utf-8",
    )
    (root / "receipt.json").write_text(
        json.dumps(
            {
                **SRT_RELEASE.receipt_fields(),
                "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(srt_runtime, "srt_install_root", lambda: root)
    monkeypatch.setattr(srt_runtime, "__file__", str(tmp_path / "srt_runtime.py"))

    with (
        caplog.at_level("WARNING"),
        pytest.raises(SrtRuntimeError, match="missing or invalid"),
    ):
        verify_srt_installation(
            run_id="run-lockout",
            provider="codex",
            policy_hash="policy-hash",
        )

    record = caplog.records[-1]
    assert record.message == "Managed SRT validation failed closed"
    assert vars(record)["run_id"] == "run-lockout"
    assert vars(record)["provider"] == "codex"
    assert vars(record)["policy_hash"] == "policy-hash"


@pytest.fixture
def srt_root(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "runtime"
    yield root
    # Installs end read-only. Restore write access so pytest's shared cleanup of
    # old temp roots, which can run in concurrent sessions, can remove them.
    for path in (root, *root.rglob("*")) if root.exists() else ():
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)


def _write_valid_srt_install(root: Path, *, helper_mode: int = 0o755) -> None:
    from tests.srt_fixture_helpers import write_srt_proxy_fixture

    write_srt_proxy_fixture(root)
    package_dir = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    package_dir.mkdir(parents=True, exist_ok=True)
    for architecture in ("arm64", "x64"):
        helper = package_dir / "vendor" / "seccomp" / architecture / "apply-seccomp"
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"executable helper")
        helper.chmod(helper_mode)
    (package_dir / "package.json").write_text(
        json.dumps({"name": SRT_RELEASE.package, "version": SRT_RELEASE.version}),
        encoding="utf-8",
    )
    runner_source = Path(srt_runtime.__file__).with_name("srt_runner.mjs")
    shutil.copyfile(runner_source, root / "runner.mjs")
    lock_source = Path(srt_runtime.__file__).parents[1] / "install" / "srt-package-lock.json"
    shutil.copyfile(lock_source, root / "package-lock.json")
    (root / "receipt.json").write_text(
        json.dumps(SRT_RELEASE.receipt_fields() | {"node": "/usr/bin/node"}),
        encoding="utf-8",
    )
    srt_runtime.write_srt_content_manifest(root)
    srt_runtime.make_srt_installation_immutable(root)


def _patch_srt_verification_runtime(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    monkeypatch.setattr(srt_runtime, "srt_install_root", lambda: root)
    monkeypatch.setattr(
        srt_runtime,
        "node_dependency_status",
        lambda: DependencyStatus(
            state="healthy",
            installed_version="22.12.0",
            minimum_version="22.12.0",
            expected_version=None,
            path="/usr/bin/node",
            error=None,
        ),
    )


@pytest.mark.parametrize("helper_mode", [0o644, 0o444])
def test_srt_hardening_restores_seccomp_execute_bits(
    monkeypatch: pytest.MonkeyPatch,
    srt_root: Path,
    helper_mode: int,
) -> None:
    root = srt_root
    _write_valid_srt_install(root, helper_mode=helper_mode)
    _patch_srt_verification_runtime(monkeypatch, root)

    installation = verify_srt_installation()

    assert installation.root == root.resolve()
    package_dir = root / "node_modules/@anthropic-ai/sandbox-runtime"
    for architecture in ("arm64", "x64"):
        helper = package_dir / "vendor/seccomp" / architecture / "apply-seccomp"
        assert helper.stat().st_mode & 0o777 == 0o555
        assert helper.read_bytes() == b"executable helper"
    for path in (root / "runner.mjs", root / "receipt.json", package_dir / "package.json"):
        assert path.stat().st_mode & 0o777 == 0o444
    assert root.stat().st_mode & 0o777 == 0o555
    manifest = json.loads((root / "content-manifest.json").read_text(encoding="utf-8"))
    assert srt_runtime.build_srt_content_manifest(root) == manifest


def test_verify_srt_installation_accepts_release_contract(
    monkeypatch: pytest.MonkeyPatch,
    srt_root: Path,
) -> None:
    root = srt_root
    _write_valid_srt_install(root)
    _patch_srt_verification_runtime(monkeypatch, root)

    installation = verify_srt_installation()

    assert installation.root == root.resolve()
    assert installation.runner == (root / "runner.mjs").resolve()
    helper = root / "node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp/arm64/apply-seccomp"
    assert helper.stat().st_mode & 0o777 == 0o555


def test_verify_srt_installation_rejects_unmanifested_package_content(
    monkeypatch: pytest.MonkeyPatch,
    srt_root: Path,
) -> None:
    root = srt_root
    _write_valid_srt_install(root)
    _patch_srt_verification_runtime(monkeypatch, root)
    (root / "node_modules").chmod(0o755)
    injected = root / "node_modules" / "injected.js"
    injected.write_text("export default 'persisted payload';\n", encoding="utf-8")

    with pytest.raises(SrtRuntimeError, match="content manifest"):
        verify_srt_installation()


@pytest.mark.parametrize("name", ["http-proxy.js", "mux-proxy.js"])
def test_verify_srt_installation_rejects_unpatched_proxy_with_rewritten_manifest(
    monkeypatch: pytest.MonkeyPatch,
    srt_root: Path,
    name: str,
) -> None:
    from gobby.agents.srt_package_patch import HTTP_PROXY_PATH
    from tests.srt_fixture_helpers import upstream_proxy_bytes

    _write_valid_srt_install(srt_root)
    _patch_srt_verification_runtime(monkeypatch, srt_root)
    proxy = (srt_root / HTTP_PROXY_PATH).with_name(name)
    proxy.chmod(0o644)
    proxy.write_bytes(upstream_proxy_bytes(name.removesuffix(".js")))
    (srt_root / "content-manifest.json").chmod(0o644)
    srt_runtime.write_srt_content_manifest(srt_root)
    srt_runtime.make_srt_installation_immutable(srt_root)

    with pytest.raises(SrtRuntimeError, match=f"{name} checksum mismatch"):
        verify_srt_installation()


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    [
        ("receipt", "receipt does not match"),
        ("version", "package identity"),
        ("runner", "runner checksum"),
        ("helper-mode", "seccomp helper is not executable"),
        ("helper-content", "content manifest mismatch"),
    ],
)
def test_verify_srt_installation_rejects_corruption(
    monkeypatch: pytest.MonkeyPatch,
    srt_root: Path,
    corruption: str,
    expected_error: str,
) -> None:
    root = srt_root
    _write_valid_srt_install(root)
    _patch_srt_verification_runtime(monkeypatch, root)
    if corruption == "receipt":
        (root / "receipt.json").chmod(0o644)
        receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
        receipt["tarball_sha256"] = "wrong"
        (root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    elif corruption == "version":
        package_json = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "package.json"
        package_json.chmod(0o644)
        package_json.write_text(
            json.dumps({"name": SRT_RELEASE.package, "version": "0.0.65"}),
            encoding="utf-8",
        )
    elif corruption in ("helper-mode", "helper-content"):
        helper = (
            root / "node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp/arm64/apply-seccomp"
        )
        if corruption == "helper-mode":
            helper.chmod(0o444)
        else:
            helper.chmod(0o644)
            helper.write_bytes(b"corrupted helper")
            helper.chmod(0o555)
    else:
        (root / "runner.mjs").chmod(0o644)
        (root / "runner.mjs").write_text("corrupted", encoding="utf-8")

    with pytest.raises(SrtRuntimeError, match=expected_error):
        verify_srt_installation()


@pytest.mark.asyncio
async def test_srt_verification_does_not_run_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verifying the pinned install rehashes node_modules; that cannot hold the loop.

    build_srt_content_manifest walks the whole managed tree, SHA-256s every file
    and then stats each one again -- 826 files and 44ms on this machine, warm, at
    rest. Running it inline made every spawn a multi-hundred-millisecond stall
    under load, which is what the loop-lag watchdog caught as
    _verify_srt_content -> Path.stat on the loop thread (#20841).
    """
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
    provider_target = tmp_path / "claude-bin" / "claude"
    provider_target.parent.mkdir(parents=True)
    provider_target.write_text("#!/bin/sh\n", encoding="utf-8")
    provider_target.chmod(0o755)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "claude").symlink_to(provider_target)
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    verified_on: list[int] = []

    def verified_installation(**_context: str | None) -> SrtInstallation:
        verified_on.append(threading.get_ident())
        return SrtInstallation(runtime, node, runner, package_json)

    async def fake_preflight(launch: SandboxLaunch, cwd: str, env: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(srt_runtime, "verify_srt_installation", verified_installation)
    monkeypatch.setattr(srt_runtime, "_preflight_srt", fake_preflight)
    original_which = shutil.which

    def fake_which(
        command: str,
        mode: int = os.F_OK | os.X_OK,
        path: str | None = None,
    ) -> str | None:
        if command == "claude":
            return str(shim_dir / "claude")
        result = original_which(command, mode=mode, path=path)
        return None if result is None else str(result)

    monkeypatch.setattr(shutil, "which", fake_which)

    await prepare_sandbox_launch(
        config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
        provider="claude",
        workspace_path=str(workspace),
        run_id="offloop",
        resolver=None,
        daemon_port=60887,
        websocket_port=60888,
        api_base=None,
        env={"PATH": str(shim_dir)},
    )

    assert len(verified_on) == 1
    assert verified_on[0] != threading.get_ident()


async def _swap_then_compute(
    swap: Callable[[], None], *args: Any, **kwargs: Any
) -> ResolvedSandboxPaths:
    swap()
    return await compute_sandbox_paths(*args, **kwargs)


async def _compute_then_swap(
    swap: Callable[[], None], *args: Any, **kwargs: Any
) -> ResolvedSandboxPaths:
    paths = await compute_sandbox_paths(*args, **kwargs)
    swap()
    return paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "race", [_swap_then_compute, _compute_then_swap], ids=["before-grants", "after-grants"]
)
async def test_prepare_srt_launch_refuses_a_cargo_home_swapped_in_at_render(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    race: Callable[..., Awaitable[ResolvedSandboxPaths]],
) -> None:
    """Every concurrent sandboxed run holds the cargo-home grant, so one can swap the
    entry for a link between run-path preparation and policy rendering. The launch
    must fail closed instead of granting the link's target (#23194)."""
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    unsandboxed = tmp_path / "unsandboxed-cargo-home"
    unsandboxed.mkdir()
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    provider = shim_dir / "claude"
    provider.write_text("#!/bin/sh\n", encoding="utf-8")
    provider.chmod(0o755)
    cargo_home = gobby_home / "cache" / "sandbox" / "cargo-home"

    def swap() -> None:
        cargo_home.rmdir()
        cargo_home.symlink_to(unsandboxed, target_is_directory=True)

    def unexpected_verify(**_context: str | None) -> SrtInstallation:
        pytest.fail("no policy may be rendered for a swapped cache grant")

    monkeypatch.setattr(
        "gobby.agents.sandbox.compute_sandbox_paths",
        lambda *args, **kwargs: race(swap, *args, **kwargs),
    )
    monkeypatch.setattr(srt_runtime, "verify_srt_installation", unexpected_verify)

    with pytest.raises(SrtRuntimeError, match="outside the sandbox cache"):
        await prepare_sandbox_launch(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            provider="claude",
            workspace_path=str(workspace),
            run_id="run-1",
            resolver=None,
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env={"PATH": str(shim_dir)},
        )

    assert cargo_home.is_symlink()
    assert list(unsandboxed.iterdir()) == []
    assert list(gobby_home.rglob("settings.json")) == []


@pytest.mark.asyncio
async def test_prepare_srt_launch_keeps_managed_grant_assets_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The daemon owns grant publication; clients need no lock write grant."""
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(srt_runtime, "sys", SimpleNamespace(platform="darwin"))
    gobby_home = tmp_path / "gobby-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    run_root = gobby_home / "runtime" / "managed-executions" / "exec-1"
    run_root.mkdir(parents=True)
    grant_path = run_root / "grant.json"
    grant_path.write_text("{}", encoding="utf-8")

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    node = runtime / "node"
    runner = runtime / "runner.mjs"
    package_json = runtime / "package.json"
    for path in (node, runner, package_json):
        path.write_text("test", encoding="utf-8")
    provider_target = tmp_path / "claude-bin" / "claude"
    provider_target.parent.mkdir()
    provider_target.write_text("#!/bin/sh\n", encoding="utf-8")
    provider_target.chmod(0o755)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "claude").symlink_to(provider_target)

    def verified_installation(**_context: str | None) -> SrtInstallation:
        return SrtInstallation(runtime, node, runner, package_json)

    async def fake_preflight(
        launch: SandboxLaunch,
        cwd: str,
        env: dict[str, str],
    ) -> None:
        return None

    monkeypatch.setattr(srt_runtime, "verify_srt_installation", verified_installation)
    monkeypatch.setattr(srt_runtime, "_preflight_srt", fake_preflight)

    launch = await prepare_sandbox_launch(
        config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
        provider="claude",
        workspace_path=str(workspace),
        run_id="exec-1",
        resolver=ClaudeSandboxResolver(),
        daemon_port=60887,
        websocket_port=60888,
        api_base=None,
        env={
            "PATH": str(shim_dir),
            "GOBBY_MANAGED_EXECUTION_BOOTSTRAP": str(grant_path),
        },
    )

    policy = json.loads(Path(launch.policy_path or "").read_text(encoding="utf-8"))
    allowed_writes = policy["filesystem"]["allowWrite"]
    lock = str(run_root.resolve() / "grant.json.lock")
    assert lock not in allowed_writes
    assert str(grant_path.resolve()) not in allowed_writes
    assert str(run_root.resolve()) not in allowed_writes
    assert str(grant_path.resolve()) in policy["filesystem"]["allowRead"]


class _StopAfterRunPaths(Exception):
    pass


@pytest.mark.parametrize("allow_run_sockets", [False, True])
async def test_linux_grok_refused_before_run_state_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allow_run_sockets: bool
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(srt_runtime, "_resolve_provider_executable", lambda *_: "grok")

    def unexpected_run_paths(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Grok run state prepared before Linux auth persistence refusal")

    monkeypatch.setattr(srt_runtime, "prepare_sandbox_run_paths", unexpected_run_paths)
    with pytest.raises(SrtRuntimeError, match="Linux.*Grok.*auth"):
        await prepare_sandbox_launch(
            config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
            provider="grok",
            workspace_path=str(tmp_path),
            run_id="linux-grok",
            resolver=None,
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env={},
            allow_run_unix_sockets=allow_run_sockets,
        )


async def test_disabled_linux_grok_keeps_unenforced_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    launch = await prepare_sandbox_launch(
        config=SandboxConfig(enabled=False, backend="srt"),
        provider="grok",
        workspace_path=str(tmp_path),
        run_id="linux-grok-disabled",
        resolver=None,
        daemon_port=60887,
        websocket_port=60888,
        api_base=None,
        env={},
    )
    assert not launch.enforced
    assert launch.backend == "srt"


async def test_prepare_sandbox_launch_forwards_pre_commit_prewarm_opt_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    forwarded: list[bool] = []

    def fake_run_paths(
        run_id: str,
        env: dict[str, str],
        *,
        workspace: Path,
        short_tmp: bool = False,
        prewarm_pre_commit_store: bool = True,
    ) -> None:
        forwarded.append(prewarm_pre_commit_store)
        raise _StopAfterRunPaths

    monkeypatch.setattr(
        srt_runtime, "_resolve_provider_executable", lambda _provider, _env: "claude"
    )
    monkeypatch.setattr(srt_runtime, "prepare_sandbox_run_paths", fake_run_paths)
    config = SandboxConfig(enabled=True, backend="srt", allow_network=False)

    with pytest.raises(_StopAfterRunPaths):
        await prepare_sandbox_launch(
            config=config,
            provider="claude",
            workspace_path=str(tmp_path),
            run_id="run-default",
            resolver=None,
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env={},
        )
    with pytest.raises(_StopAfterRunPaths):
        await prepare_sandbox_launch(
            config=config,
            provider="claude",
            workspace_path=str(tmp_path),
            run_id="run-opt-out",
            resolver=None,
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env={},
            prewarm_pre_commit_store=False,
        )

    assert forwarded == [True, False]


class _FakePreflightProcess:
    def __init__(self, *, returncode: int = 0, stderr: bytes = b"") -> None:
        self._final_returncode = returncode
        self._stderr = stderr
        self.returncode: int | None = None
        self.killed = False
        self.reaped = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.returncode = self._final_returncode
        return b"", self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int | None:
        self.reaped = True
        return self.returncode


def _install_fake_preflight(
    monkeypatch: pytest.MonkeyPatch,
    process: _FakePreflightProcess,
    *,
    simulated_seconds: float,
) -> None:
    """Run the preflight against a fake that takes ``simulated_seconds`` to finish.

    The fake ``wait_for`` times out exactly when the bound is below the simulated
    duration, so slow preflights are exercised without sleeping in the test.
    """

    async def fake_exec(*_command: str, **_kwargs: Any) -> _FakePreflightProcess:
        return process

    async def simulated_wait_for(
        awaitable: Coroutine[Any, Any, tuple[bytes, bytes]], timeout: float
    ) -> tuple[bytes, bytes]:
        if timeout < simulated_seconds:
            awaitable.close()
            raise TimeoutError
        return await awaitable

    monkeypatch.setattr(spawn, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        srt_runtime,
        "asyncio",
        SimpleNamespace(
            wait_for=simulated_wait_for,
            subprocess=asyncio.subprocess,
            CancelledError=asyncio.CancelledError,
        ),
    )


async def test_preflight_slower_than_twenty_seconds_passes_within_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _FakePreflightProcess()
    _install_fake_preflight(monkeypatch, process, simulated_seconds=25.0)

    await srt_runtime._preflight_srt(_enforced_srt_launch(), str(tmp_path), {})

    assert process.returncode == 0
    assert not process.killed


async def test_cancelled_preflight_kills_and_reaps_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _FakePreflightProcess()
    communicating = asyncio.Event()

    async def blocked_communicate() -> tuple[bytes, bytes]:
        communicating.set()
        await asyncio.Event().wait()
        return b"", b""

    monkeypatch.setattr(process, "communicate", blocked_communicate)
    _install_fake_preflight(monkeypatch, process, simulated_seconds=1.0)
    task = asyncio.create_task(
        srt_runtime._preflight_srt(_enforced_srt_launch(), str(tmp_path), {})
    )
    await communicating.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed
    assert process.reaped


async def test_preflight_over_ceiling_fails_closed_with_elapsed_and_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _FakePreflightProcess()
    _install_fake_preflight(monkeypatch, process, simulated_seconds=120.0)

    with pytest.raises(
        SrtRuntimeError,
        match=r"^managed SRT preflight timed out after \d+\.\ds \(bound 90s\)$",
    ):
        await srt_runtime._preflight_srt(_enforced_srt_launch(), str(tmp_path), {})

    assert process.killed
    assert process.reaped


async def test_preflight_nonzero_exit_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _FakePreflightProcess(returncode=1, stderr=b"sandbox denied\n")
    _install_fake_preflight(monkeypatch, process, simulated_seconds=1.0)

    with pytest.raises(SrtRuntimeError, match=r"^managed SRT preflight failed: sandbox denied$"):
        await srt_runtime._preflight_srt(_enforced_srt_launch(), str(tmp_path), {})

    assert not process.killed
