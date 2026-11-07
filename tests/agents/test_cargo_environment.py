"""Every agent launch path builds plain incremental, with no sccache wiring.

Operator decision A on #25825 removed Rust sccache: its Rust compiles are keyed on the
target path, so separate checkouts never shared a hit, and its edit loop was slower.
Cargo reads any set CARGO_INCREMENTAL, blank included, as incremental off, and the gterm
host hands each child the daemon's inherited environment, so agent launches remove
these keys at exec instead of blanking them.

Socket-present cases keep their roots directly under the system temp root, as live
launches do: pytest's tmp_path exceeds macOS's 103-byte socket path limit.
"""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.agents import srt_runtime
from gobby.agents.cargo_environment import cargo_environment
from gobby.agents.cargo_target import ensure_agent_cargo_target_dir
from gobby.agents.constants import GOBBY_SESSION_ID, UV_CACHE_DIR
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_run_environment import SandboxRunPaths
from gobby.agents.spawn_cache_policy import build_spawn_cache_env, sandbox_config_for_spawn
from gobby.agents.srt_runtime import SandboxLaunch, SrtInstallation, prepare_sandbox_launch
from gobby.terminals.native_env_policy import apply_native_env_policy

pytestmark = pytest.mark.unit

REMOVED_KEYS = ("CARGO_INCREMENTAL", "RUSTC_WRAPPER", "SCCACHE_DIR", "SCCACHE_SERVER_UDS")
REPORT_REMOVED_KEYS = (
    "for k in CARGO_INCREMENTAL RUSTC_WRAPPER SCCACHE_DIR SCCACHE_SERVER_UDS; "
    'do printenv "$k" || echo absent; done'
)


@pytest.fixture
def fake_sccache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sccache = bin_dir / "sccache"
    sccache.write_text("#!/bin/sh\n", encoding="utf-8")
    sccache.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return sccache


def _wired(env: Mapping[str, str]) -> list[str]:
    # Present at all, blank included: a blank CARGO_INCREMENTAL still turns incremental off.
    return [key for key in REMOVED_KEYS if key in env]


def _run_paths(root: Path) -> SandboxRunPaths:
    return SandboxRunPaths(
        root=root,
        assets=root / "assets",
        tmp=root / "tmp",
        hooks=root / "hooks",
        logs=root / "logs",
        cache=root / "cache",
        shared_cache=Path("/shared"),
        cargo_target=Path("/shared/cargo-target/checkout"),
    )


def test_launch_paths_set_no_sccache_or_incremental_keys(
    fake_sccache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # sccache on PATH wires nothing.
    assert fake_sccache.is_file()
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    launches = {
        "cargo_environment": cargo_environment(),
        "sandboxed": _run_paths(Path("/run")).environment("codex"),
        "unsandboxed": build_spawn_cache_env("session"),
    }

    for name, env in launches.items():
        assert _wired(env) == [], name
        assert env["CARGO_BUILD_JOBS"] == "4", name
    # The unsandboxed spawn no longer creates the shared sccache folder.
    assert not (gobby_home / "cache" / "sccache").exists()


@pytest.mark.parametrize(
    "command",
    [
        ["codex", "--full-auto"],
        ["/managed/node", "/managed/srt/runner.js", "--", "/opt/bin/claude"],
        ["agy", "--yolo"],
    ],
)
def test_agent_exec_removes_the_cargo_keys(command: list[str]) -> None:
    caller_env = {**dict.fromkeys(REMOVED_KEYS, "0"), "EDITOR": "vi"}

    argv, env = apply_native_env_policy(command, caller_env, None)

    separator = argv.index("--")
    unset = argv[1:separator]
    assert argv[0] == "/usr/bin/env"
    assert set(REMOVED_KEYS) <= set(unset[1::2])
    assert unset[::2] == ["-u"] * (len(unset) // 2)
    assert argv[separator + 1 :] == command
    assert _wired(env) == []
    assert env["EDITOR"] == "vi"


def test_agent_exec_drops_an_inherited_cargo_incremental() -> None:
    argv, env = apply_native_env_policy(["/bin/sh", "-c", REPORT_REMOVED_KEYS], {}, "codex")
    # Stands in for the gterm host's inherited daemon environment.
    host_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        **dict.fromkeys(REMOVED_KEYS, "0"),
        **env,
    }

    result = subprocess.run(
        argv, env=host_env, capture_output=True, text=True, timeout=10, check=True
    )

    assert result.stdout.splitlines() == ["absent"] * len(REMOVED_KEYS)


def test_non_agent_exec_keeps_its_cargo_environment() -> None:
    argv, env = apply_native_env_policy(["/bin/zsh"], {"CARGO_INCREMENTAL": "1"}, None)

    assert argv == ["/bin/zsh"]
    assert env["CARGO_INCREMENTAL"] == "1"


@pytest.mark.parametrize(
    ("platform_name", "allow_run_sockets", "socket_granted"),
    [("darwin", True, True), ("darwin", False, False), ("linux", True, False)],
)
async def test_sandbox_launch_builds_plain_incremental(
    fake_sccache: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    allow_run_sockets: bool,
    socket_granted: bool,
) -> None:
    monkeypatch.setattr(srt_runtime, "sys", SimpleNamespace(platform=platform_name))
    monkeypatch.setattr(srt_runtime, "srt_mux_tmpdir", lambda: tmp_path / "srt-sock")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(srt_runtime, "_resolve_provider_executable", lambda *_: str(executable))

    def installation(**_context: str | None) -> SrtInstallation:
        return SrtInstallation(tmp_path, executable, executable, executable)

    async def preflight(launch: SandboxLaunch, cwd: str, env: dict[str, str]) -> None:
        return None

    def monitor(**_kwargs: object) -> str:
        return str(tmp_path / "monitor.sock")

    monkeypatch.setattr(srt_runtime, "verify_srt_installation", installation)
    monkeypatch.setattr(srt_runtime, "_preflight_srt", preflight)
    monkeypatch.setattr("gobby.agents.srt_monitor.ensure_srt_monitor", monitor)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    system_tmp = tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="", dir=system_tmp) as short:
        gobby_home = Path(short)
        monkeypatch.setenv("GOBBY_HOME", short)
        # Session caches stay in tmp_path. The run's short tmp goes under the
        # system temp root, as on a live launch.
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        spawn_env = {GOBBY_SESSION_ID: "session", **build_spawn_cache_env("session")}
        # The spawn path derives the sandbox's extra write grants from its env.
        config = sandbox_config_for_spawn(
            SandboxConfig(enabled=True, backend="srt", allow_network=False), spawn_env
        )
        assert config is not None
        monkeypatch.setattr("tempfile.tempdir", system_tmp)
        launch = await prepare_sandbox_launch(
            config=config,
            provider="codex",
            workspace_path=str(workspace),
            run_id="cargo-cache",
            resolver=None,
            daemon_port=60887,
            websocket_port=60888,
            api_base=None,
            env=spawn_env,
            allow_run_unix_sockets=allow_run_sockets,
            prewarm_pre_commit_store=False,
        )
        run_tmp = Path(launch.provider_env["TMPDIR"])
        try:
            policy = json.loads(Path(launch.policy_path or "").read_text(encoding="utf-8"))
            writes = [Path(path) for path in policy["filesystem"]["allowWrite"]]
            sockets = [Path(path) for path in policy["network"]["allowUnixSockets"]]
            # Launch callers merge the provider env over the spawn env.
            child_env = {**spawn_env, **launch.provider_env}
            assert fake_sccache.is_file()
            assert _wired(child_env) == []
            assert child_env["CARGO_BUILD_JOBS"] == "4"
            assert Path(child_env["CARGO_HOME"]).resolve() in writes
            # The run tmp socket grant predates sccache (#21752) and stays for run sockets.
            assert (run_tmp.resolve() in sockets) == socket_granted
            assert not (gobby_home / "cache" / "sccache").exists()
        finally:
            shutil.rmtree(run_tmp, ignore_errors=True)


def test_spawn_cargo_caches_are_never_granted_for_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    # A live spawn env carries the unsandboxed CARGO_TARGET_DIR beside the shared cache env.
    spawn_env = {
        GOBBY_SESSION_ID: "session",
        **build_spawn_cache_env("session"),
        "CARGO_TARGET_DIR": ensure_agent_cargo_target_dir(checkout, "project-id"),
    }
    config = sandbox_config_for_spawn(
        SandboxConfig(enabled=True, backend="srt", allow_network=False), spawn_env
    )
    assert config is not None
    # The spawn env's CARGO_HOME and CARGO_TARGET_DIR belong to the unsandboxed trust domain.
    # The run's own cargo home and target are granted by the launch, so no grant may overlap
    # either unsandboxed root, nor the cargo-target-v2 root that holds every seat's target.
    target_root = gobby_home / "cache" / "cargo-target-v2"
    unsandboxed_roots = (Path(spawn_env["CARGO_HOME"]), target_root)
    assert Path(spawn_env["CARGO_TARGET_DIR"]).is_relative_to(target_root)
    exposed = [
        granted
        for granted in config.extra_write_paths
        for root in unsandboxed_roots
        if Path(granted).resolve().is_relative_to(root.resolve())
        or root.resolve().is_relative_to(Path(granted).resolve())
    ]
    assert exposed == []


def test_sandbox_never_grants_the_unsandboxed_session_uv_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    # Unsandboxed agents install into the session uv cache, so no sandbox may be granted it.
    spawn_env = {GOBBY_SESSION_ID: "session", **build_spawn_cache_env("session")}
    unsandboxed = Path(spawn_env[UV_CACHE_DIR])
    config = sandbox_config_for_spawn(
        SandboxConfig(enabled=True, backend="srt", allow_network=False), spawn_env
    )
    assert config is not None
    exposed = [
        granted
        for granted in config.extra_write_paths
        if Path(granted).resolve().is_relative_to(unsandboxed.resolve())
        or unsandboxed.resolve().is_relative_to(Path(granted).resolve())
    ]
    assert exposed == []


def test_sandbox_refuses_a_spawn_without_a_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    # Without an id, every such spawn would share one fallback cache directory.
    with pytest.raises(ValueError, match=GOBBY_SESSION_ID):
        sandbox_config_for_spawn(
            SandboxConfig(enabled=True, backend="srt", allow_network=False),
            build_spawn_cache_env("session"),
        )
