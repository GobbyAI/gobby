"""Focused tests for the managed Sandbox Runtime backend."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from gobby.agents import srt_runtime
from gobby.agents.sandbox import (
    ClaudeSandboxResolver,
    CodexSandboxResolver,
    GrokSandboxResolver,
    QwenSandboxResolver,
    ResolvedSandboxPaths,
    SandboxConfig,
    SandboxCredentialEnv,
    SandboxResolver,
    compute_sandbox_paths,
)
from gobby.agents.sandbox_policy import _nearest_package_root
from gobby.agents.srt_runtime import (
    SandboxLaunch,
    SrtInstallation,
    SrtRuntimeError,
    prepare_sandbox_launch,
    render_srt_settings,
    verify_srt_installation,
)
from gobby.utils.dependency_requirements import (
    SRT_RELEASE,
    DependencyStatus,
)

pytestmark = pytest.mark.unit


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
    assert str((home / ".gobby" / "bootstrap.yaml").resolve()) in filesystem["denyRead"]
    assert str((home / ".gobby").resolve()) not in filesystem["denyRead"]


def test_provider_state_roots_are_writable(
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
        paths = compute_sandbox_paths(
            SandboxConfig(enabled=True, backend="srt", allow_network=False),
            str(workspace),
            provider=provider,
            env={"PATH": ""},
        )
        filesystem = render_srt_settings(paths)["filesystem"]
        state_path = str((home / state_root).resolve())

        assert state_path in filesystem["allowWrite"]
        assert state_path in filesystem["allowRead"]
        assert str((home / ".ssh").resolve()) in filesystem["denyWrite"]

        gobby_home = (home / ".gobby").resolve()
        assert str(gobby_home) not in filesystem["allowRead"]
        assert str(gobby_home) not in filesystem["allowWrite"]
        assert str(gobby_home / "bootstrap.yaml") in filesystem["denyRead"]

        uv_root = str((home / ".local" / "share" / "uv").resolve())
        assert uv_root in filesystem["allowRead"]
        assert uv_root not in filesystem["allowWrite"]


def test_claude_account_auth_files_are_read_only_sandbox_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))

    paths = compute_sandbox_paths(
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
    # Both flags are network capabilities only. Local toolchain caches are
    # writable either way: an offline `cargo build` still takes the
    # $CARGO_HOME/.package-cache lock (#19443).
    assert set(capable_paths.write_paths) == set(default_paths.write_paths)


def test_network_capabilities_are_preserved_without_a_provider(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    paths = compute_sandbox_paths(
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
    ("provider", "temp_env_name"),
    [("claude", "CLAUDE_CODE_TMPDIR"), ("codex", "TMPDIR")],
)
@pytest.mark.asyncio
async def test_prepare_srt_launch_writes_private_policy_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
    temp_env_name: str,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    workspace = tmp_path / "workspace"
    untrusted_mcp_root = tmp_path / "untrusted-mcp-root"
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

    launch = await prepare_sandbox_launch(
        config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
        provider=provider,
        workspace_path=str(workspace),
        run_id="run/unsafe id",
        resolver=None,
        daemon_port=60887,
        websocket_port=60888,
        api_base=None,
        env={"PATH": str(shim_dir)},
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
    temp_path = expected_parent / "tmp"
    assert launch.provider_env[temp_env_name] == str(temp_path)
    mux_dir = gobby_home / "runtime" / "srt-sock"
    assert launch.provider_env["GOBBY_SRT_TMPDIR"] == str(mux_dir)
    assert mux_dir.is_dir()
    assert mux_dir.stat().st_mode & 0o777 == 0o700
    assert Path(launch.provider_env["UV_CACHE_DIR"]).is_relative_to(expected_parent / "cache")
    assert Path(launch.provider_env["CARGO_HOME"]).is_relative_to(expected_parent / "cache")
    for writable_name in ("tmp", "hooks", "logs", "cache"):
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
    assert preflight_env == {"PATH": str(shim_dir), **launch.provider_env}
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    allowed_reads = policy["filesystem"]["allowRead"]
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
        ("qwen", QwenSandboxResolver()),
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


def _write_valid_srt_install(root: Path) -> None:
    package_dir = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    package_dir.mkdir(parents=True)
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
            installed_version="20.11.0",
            minimum_version="20.11.0",
            expected_version=None,
            path="/usr/bin/node",
            error=None,
        ),
    )


def test_verify_srt_installation_accepts_release_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    _write_valid_srt_install(root)
    _patch_srt_verification_runtime(monkeypatch, root)

    installation = verify_srt_installation()

    assert installation.root == root.resolve()
    assert installation.runner == (root / "runner.mjs").resolve()


def test_verify_srt_installation_rejects_unmanifested_package_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    _write_valid_srt_install(root)
    _patch_srt_verification_runtime(monkeypatch, root)
    (root / "node_modules").chmod(0o755)
    injected = root / "node_modules" / "injected.js"
    injected.write_text("export default 'persisted payload';\n", encoding="utf-8")

    with pytest.raises(SrtRuntimeError, match="content manifest"):
        verify_srt_installation()


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    [
        ("receipt", "receipt does not match"),
        ("version", "package identity"),
        ("runner", "runner checksum"),
    ],
)
def test_verify_srt_installation_rejects_corruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    corruption: str,
    expected_error: str,
) -> None:
    root = tmp_path / "runtime"
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
