"""Both agent launch paths build through one sccache and Cargo job-limit environment.

Socket-present cases keep their roots directly under the system temp root, as live
launches do: pytest's tmp_path exceeds macOS's 103-byte socket path limit.
"""

import json
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.agents import srt_runtime
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_run_environment import SandboxRunPaths
from gobby.agents.spawn_cache_policy import build_spawn_cache_env, sandbox_config_for_spawn
from gobby.agents.srt_runtime import SandboxLaunch, SrtInstallation, prepare_sandbox_launch

pytestmark = pytest.mark.unit

SCCACHE_KEYS = ("RUSTC_WRAPPER", "SCCACHE_DIR", "SCCACHE_SERVER_UDS")
BLANK = ("", "", "")


@pytest.fixture
def fake_sccache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sccache = bin_dir / "sccache"
    sccache.write_text("#!/bin/sh\n", encoding="utf-8")
    sccache.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return sccache


def _sccache_values(env: Mapping[str, str]) -> tuple[str | None, ...]:
    # Blank, never omitted: callers merge this over an env that may hold other values.
    return tuple(env.get(key) for key in SCCACHE_KEYS)


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


def test_launch_paths_share_one_cargo_environment(
    fake_sccache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandboxed = _run_paths(Path("/run"))
    granted = sandboxed.environment("codex", run_sockets=True)
    assert _sccache_values(granted) == (
        str(fake_sccache),
        "/shared/cargo-home/sccache",
        "/run/tmp/sccache.sock",
    )
    assert granted["CARGO_BUILD_JOBS"] == "4"
    ungranted = sandboxed.environment("codex")
    assert _sccache_values(ungranted) == BLANK
    assert ungranted["CARGO_BUILD_JOBS"] == "4"
    long_socket = _run_paths(Path("/run") / ("r" * 100))
    assert _sccache_values(long_socket.environment("codex", run_sockets=True)) == BLANK

    with tempfile.TemporaryDirectory(prefix="", dir=tempfile.gettempdir()) as short:
        home = Path(short)
        monkeypatch.setenv("GOBBY_HOME", short)
        monkeypatch.setattr("tempfile.tempdir", short)
        unsandboxed = build_spawn_cache_env("session")
        cache = home / "cache" / "sccache"
        assert _sccache_values(unsandboxed) == (
            str(fake_sccache),
            str(cache),
            str(home / "cache" / "sccache.sock"),
        )
        assert cache.is_dir()
        assert unsandboxed["CARGO_BUILD_JOBS"] == "4"

        monkeypatch.setenv("GOBBY_HOME", str(home / ("h" * 100)))
        long_home = build_spawn_cache_env("session")
        assert _sccache_values(long_home) == BLANK
        assert long_home["CARGO_BUILD_JOBS"] == "4"

        monkeypatch.setenv("GOBBY_HOME", short)
        monkeypatch.setenv("PATH", "")
        assert _sccache_values(build_spawn_cache_env("session")) == BLANK
        assert _sccache_values(sandboxed.environment("codex", run_sockets=True)) == BLANK


@pytest.mark.parametrize(
    ("platform_name", "allow_run_sockets", "cached"),
    [("darwin", True, True), ("darwin", False, False), ("linux", True, False)],
)
async def test_sandbox_cache_grants_keep_the_trust_split(
    fake_sccache: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    allow_run_sockets: bool,
    cached: bool,
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
        spawn_env = build_spawn_cache_env("session")
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
            if cached:
                cargo_home = Path(child_env["CARGO_HOME"])
                wrapper, cache_dir, socket = _sccache_values(child_env)
                assert (wrapper, cache_dir) == (str(fake_sccache), str(cargo_home / "sccache"))
                assert cargo_home.resolve() in writes
                assert Path(socket or "") == run_tmp / "sccache.sock"
                assert run_tmp.resolve() in sockets
            else:
                assert _sccache_values(child_env) == BLANK
            assert child_env["CARGO_BUILD_JOBS"] == "4"
            unsandboxed = (
                Path(spawn_env["SCCACHE_DIR"]),
                Path(spawn_env["SCCACHE_SERVER_UDS"]),
            )
            assert unsandboxed == (
                gobby_home / "cache" / "sccache",
                gobby_home / "cache" / "sccache.sock",
            )
            exposed = [
                granted
                for granted in (*writes, *sockets)
                for path in unsandboxed
                if path.resolve().is_relative_to(granted.resolve())
            ]
            assert exposed == []
        finally:
            shutil.rmtree(run_tmp, ignore_errors=True)
