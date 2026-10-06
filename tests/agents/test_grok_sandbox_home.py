"""Synthetic configuration only: no operator auth or hook files are accessed."""

import sys
from pathlib import Path

import pytest

from gobby.agents import srt_runtime
from gobby.agents.grok_sandbox_home import prepare_grok_sandbox_home
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.agents.srt_runtime import SandboxLaunch, SrtInstallation, prepare_sandbox_launch

pytestmark = pytest.mark.unit


@pytest.fixture
def grok_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    source = home / ".grok"
    source.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
    monkeypatch.setattr(Path, "home", lambda: home)
    (source / "config.toml").write_text("[compat.claude]\nhooks = false\n")
    (source / "managed_config.toml").write_text("# synthetic policy cache\n")
    (source / "requirements.toml").write_text("# synthetic requirements\n")
    (source / "managed_config.sig.json").write_text('{"synthetic": true}')
    (source / "auth.json").write_text('{"synthetic": true}')
    (source / "hooks").mkdir()
    (source / "hooks/gobby.json").write_text('{"hooks": {}}')
    return source


def test_config_caches_are_independent_and_auth_is_linked(
    grok_source: Path, tmp_path: Path
) -> None:
    prepared = prepare_grok_sandbox_home(tmp_path / "run-cache", {}, assets=tmp_path / "assets")
    assert prepared.home != grok_source
    for name in (
        "config.toml",
        "managed_config.toml",
        "requirements.toml",
        "managed_config.sig.json",
    ):
        target = prepared.home / name
        assert not target.is_symlink()
        assert target.read_bytes() == (grok_source / name).read_bytes()
    assert (prepared.home / "hooks/gobby.json").read_text() == '{"hooks": {}}'
    assert (prepared.home / "auth.json").is_symlink()
    assert (prepared.home / "auth.json").resolve() == grok_source / "auth.json"
    (prepared.home / "managed_config.toml").write_text("# synthetic runtime refresh\n")
    assert (grok_source / "managed_config.toml").read_text() == "# synthetic policy cache\n"


def test_existing_run_configuration_is_retained(grok_source: Path, tmp_path: Path) -> None:
    cache = tmp_path / "run-cache"
    prepared = prepare_grok_sandbox_home(cache, {}, assets=tmp_path / "assets")
    (prepared.home / "requirements.toml").write_text("# synthetic refreshed cache\n")
    again = prepare_grok_sandbox_home(cache, {}, assets=tmp_path / "assets")
    assert again.home == prepared.home
    assert (again.home / "requirements.toml").read_text() == "# synthetic refreshed cache\n"


def test_runtime_state_remains_shared_and_configured_sources_protected(
    grok_source: Path, tmp_path: Path
) -> None:
    source = tmp_path / "custom-grok"
    source.mkdir()
    (source / "hooks-paths").write_text(str(tmp_path / "custom-hook.json") + "\n")
    prepared = prepare_grok_sandbox_home(
        tmp_path / "run-cache", {"GROK_HOME": str(source)}, assets=tmp_path / "assets"
    )
    assert str(source / "config.toml") in prepared.protected_write_paths
    assert str(tmp_path / "custom-hook.json") in prepared.protected_write_paths
    sessions = prepared.home / "sessions"
    assert sessions.is_symlink()
    assert sessions.resolve() == source / "sessions"
    assert str(source / "sessions") in prepared.runtime_write_paths


async def test_host_root_and_binary_are_unwritable_with_runtime_children_retained(
    grok_source: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = await compute_sandbox_paths(
        SandboxConfig(enabled=True, backend="srt"),
        str(workspace),
        provider="grok",
        env={"PATH": ""},
    )
    assert str(grok_source) not in paths.write_paths
    assert not any((grok_source / "bin/grok").is_relative_to(Path(p)) for p in paths.write_paths)
    assert str(grok_source / "sessions") in paths.write_paths
    assert str(grok_source / "logs") in paths.write_paths


@pytest.mark.parametrize("auth_exists", [True, False])
async def test_launch_uses_private_grok_home_and_protects_host_controls(
    grok_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_exists: bool
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    if not auth_exists:
        (grok_source / "auth.json").unlink()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "grok"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.setattr(srt_runtime, "_resolve_provider_executable", lambda *_: str(executable))

    def installation(**_context: str | None) -> SrtInstallation:
        return SrtInstallation(tmp_path, executable, executable, executable)

    async def preflight(launch: SandboxLaunch, cwd: str, env: dict[str, str]) -> None:
        assert env["GROK_HOME"] == launch.provider_env["GROK_HOME"]

    monkeypatch.setattr(srt_runtime, "verify_srt_installation", installation)
    monkeypatch.setattr(srt_runtime, "_preflight_srt", preflight)
    launch = await prepare_sandbox_launch(
        config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
        provider="grok",
        workspace_path=str(workspace),
        run_id="grok-test",
        resolver=None,
        daemon_port=60887,
        websocket_port=60888,
        api_base=None,
        env={"PATH": ""},
        prewarm_pre_commit_store=False,
    )
    import json

    policy = json.loads(Path(launch.policy_path or "").read_text())["filesystem"]
    private = Path(launch.provider_env["GROK_HOME"])
    assert private != grok_source
    assert (private / "config.toml").read_text() == (grok_source / "config.toml").read_text()
    assert str(grok_source) not in policy["allowWrite"]
    assert str(grok_source / "config.toml") in policy["denyWrite"]
    assert str(private / "hooks") in policy["denyWrite"]
    assert str(private / "config.toml") in policy["denyWrite"]
    assert str(private / "managed_config.toml") not in policy["denyWrite"]
    assert launch.provider_env["GROK_AUTH_PATH"] == str(private / "auth.json")
    assert str(grok_source / "auth.json") in policy["allowWrite"]
    assert str(grok_source / "auth.json.lock") in policy["allowWrite"]
    assert str(grok_source / ".*.*.tmp") in policy["allowWrite"]
    assert (private / "auth.json.lock").resolve() == grok_source / "auth.json.lock"
    for granted in (grok_source / "auth.json", grok_source / ".*.*.tmp"):
        assert not any(path.startswith(str(granted) + "/") for path in policy["denyWrite"])


def test_auth_override_uses_shared_refresh_target_and_lock(
    grok_source: Path, tmp_path: Path
) -> None:
    auth = tmp_path / "auth-store" / "custom.json"
    auth.parent.mkdir()
    auth.write_text('{"synthetic": "old-token"}')
    result = prepare_grok_sandbox_home(
        tmp_path / "cache", {"GROK_AUTH_PATH": str(auth)}, assets=tmp_path / "assets"
    )
    assert (result.home / "auth.json").resolve() == auth
    assert (result.home / "auth.json.lock").resolve() == auth.parent / "auth.json.lock"
    assert str(auth) in result.runtime_write_paths
    assert str(auth.parent) not in result.runtime_write_paths


def test_reused_home_cannot_change_auth_target(grok_source: Path, tmp_path: Path) -> None:
    cache, assets = tmp_path / "cache", tmp_path / "assets"
    prepare_grok_sandbox_home(cache, {}, assets=assets)
    other_auth = grok_source / "other-auth.json"
    other_auth.write_text("{}")
    with pytest.raises(ValueError, match="auth target changed"):
        prepare_grok_sandbox_home(cache, {"GROK_AUTH_PATH": str(other_auth)}, assets=assets)


def test_incomplete_preparation_cannot_be_reused(grok_source: Path, tmp_path: Path) -> None:
    cache = tmp_path / "run-cache"
    (cache / "grok-home").mkdir(parents=True)
    with pytest.raises(ValueError, match="completed source receipt"):
        prepare_grok_sandbox_home(cache, {}, assets=tmp_path / "assets")


def test_runtime_alias_to_user_credentials_is_rejected_before_mutation(
    grok_source: Path, tmp_path: Path
) -> None:
    credentials = grok_source.parent / ".ssh"
    credentials.mkdir()
    (grok_source / "sessions").symlink_to(credentials, target_is_directory=True)
    with pytest.raises(ValueError, match="credential deny"):
        prepare_grok_sandbox_home(tmp_path / "run-cache", {}, assets=tmp_path / "assets")
    assert not (tmp_path / "run-cache/grok-home").exists()


def test_resume_uses_read_only_source_receipt(grok_source: Path) -> None:
    run_root = grok_source.parent / ".gobby/run/sandbox"
    first = prepare_grok_sandbox_home(
        run_root / "first/cache", {}, assets=run_root / "first/assets"
    )
    # Writable run files cannot redirect the next launch's host configuration.
    (first.home / "grok-home-source.json").write_text('{"source": "/unexpected"}')
    resumed = prepare_grok_sandbox_home(
        run_root / "second/cache",
        {"GROK_HOME": str(first.home)},
        assets=run_root / "second/assets",
    )
    assert (resumed.home / "sessions").resolve() == grok_source / "sessions"
    assert str(grok_source / "config.toml") in resumed.protected_write_paths
