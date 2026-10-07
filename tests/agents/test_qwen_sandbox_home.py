"""Synthetic configuration only: no operator auth or hook files are accessed."""

import json
import sys
from pathlib import Path

import pytest

from gobby.agents import srt_runtime
from gobby.agents.qwen_sandbox_home import prepare_qwen_sandbox_home
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.srt_runtime import SandboxLaunch, SrtInstallation, prepare_sandbox_launch

pytestmark = pytest.mark.unit

_SETTINGS = '{"hooks": {"SessionStart": []}, "mcpServers": {"gobby": {"command": "gobby"}}}'


@pytest.fixture
def qwen_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    source = home / ".qwen"
    source.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
    monkeypatch.setattr(Path, "home", lambda: home)
    (source / "settings.json").write_text(_SETTINGS)
    (source / "trustedFolders.json").write_text("{}")
    (source / "oauth_creds.json").write_text('{"synthetic": true}')
    for name in ("skills", "commands", "agents", "extensions", "extension-store"):
        (source / name).mkdir()
    return source


def test_settings_are_copied_and_auth_is_linked(qwen_source: Path, tmp_path: Path) -> None:
    prepared = prepare_qwen_sandbox_home(tmp_path / "run-cache", {}, assets=tmp_path / "assets")

    settings = prepared.home / "settings.json"
    assert prepared.home != qwen_source
    assert not settings.is_symlink()
    assert settings.read_text() == _SETTINGS
    assert (prepared.home / "oauth_creds.json").is_symlink()
    assert (prepared.home / "oauth_creds.json").resolve() == qwen_source / "oauth_creds.json"
    assert not (prepared.home / "trustedFolders.json").exists()
    settings.write_text("{}")
    assert (qwen_source / "settings.json").read_text() == _SETTINGS


def test_launch_env_redirects_home_but_keeps_trust_and_transcripts_on_host(
    qwen_source: Path, tmp_path: Path
) -> None:
    prepared = prepare_qwen_sandbox_home(tmp_path / "run-cache", {}, assets=tmp_path / "assets")

    assert prepared.launch_env == {
        "QWEN_HOME": str(prepared.home),
        "QWEN_RUNTIME_DIR": str(qwen_source),
        "QWEN_CODE_TRUSTED_FOLDERS_PATH": str(qwen_source / "trustedFolders.json"),
    }


def test_operator_runtime_and_trust_locations_are_kept(qwen_source: Path, tmp_path: Path) -> None:
    env = {
        "QWEN_RUNTIME_DIR": str(tmp_path / "runtime"),
        "QWEN_CODE_TRUSTED_FOLDERS_PATH": str(tmp_path / "trust.json"),
    }

    prepared = prepare_qwen_sandbox_home(tmp_path / "run-cache", env, assets=tmp_path / "assets")

    assert prepared.launch_env == {"QWEN_HOME": str(prepared.home)}


def test_customizations_are_shared_and_extension_store_is_private(
    qwen_source: Path, tmp_path: Path
) -> None:
    prepared = prepare_qwen_sandbox_home(tmp_path / "run-cache", {}, assets=tmp_path / "assets")

    for name in ("skills", "commands", "agents"):
        assert (prepared.home / name).resolve() == qwen_source / name
    for name in ("extensions", "extension-store"):
        assert not (prepared.home / name).exists()
    protected = set(prepared.protected_write_paths)
    assert str(prepared.home / "settings.json") in protected
    assert str(prepared.home / "oauth_creds.json") in protected
    assert str(prepared.home / "extension-store") not in protected
    assert str(prepared.home / "extensions") not in protected


def test_global_context_and_memory_stay_the_operators(qwen_source: Path, tmp_path: Path) -> None:
    (qwen_source / "QWEN.md").write_text("synthetic global context")
    (qwen_source / "team.md").write_text("synthetic renamed context")

    prepared = prepare_qwen_sandbox_home(tmp_path / "run-cache", {}, assets=tmp_path / "assets")

    assert (prepared.home / "QWEN.md").read_text() == "synthetic global context"
    assert (prepared.home / "team.md").resolve() == qwen_source / "team.md"
    for absent in ("AGENTS.md", "memory.md"):
        assert (prepared.home / absent).readlink() == qwen_source / absent
        assert not (prepared.home / absent).exists()


def test_reuse_rejects_a_redirected_auth_link(qwen_source: Path, tmp_path: Path) -> None:
    cache = tmp_path / "run-cache"
    prepared = prepare_qwen_sandbox_home(cache, {}, assets=tmp_path / "assets")
    (prepared.home / "oauth_creds.json").unlink()
    (prepared.home / "oauth_creds.json").symlink_to(tmp_path / "elsewhere.json")

    with pytest.raises(ValueError, match="auth target changed"):
        prepare_qwen_sandbox_home(cache, {}, assets=tmp_path / "assets")


def test_symlinked_settings_are_rejected(qwen_source: Path, tmp_path: Path) -> None:
    (qwen_source / "settings.json").unlink()
    (qwen_source / "settings.json").symlink_to(tmp_path / "elsewhere.json")

    with pytest.raises(ValueError, match="must not be a symlink"):
        prepare_qwen_sandbox_home(tmp_path / "run-cache", {}, assets=tmp_path / "assets")


def test_existing_run_configuration_is_retained(qwen_source: Path, tmp_path: Path) -> None:
    cache = tmp_path / "run-cache"
    prepared = prepare_qwen_sandbox_home(cache, {}, assets=tmp_path / "assets")
    (prepared.home / "settings.json").write_text('{"synthetic": "run edit"}')

    again = prepare_qwen_sandbox_home(cache, {}, assets=tmp_path / "assets")

    assert again.home == prepared.home
    assert (again.home / "settings.json").read_text() == '{"synthetic": "run edit"}'


def test_incomplete_preparation_cannot_be_reused(qwen_source: Path, tmp_path: Path) -> None:
    cache = tmp_path / "run-cache"
    (cache / "qwen-home").mkdir(parents=True)

    with pytest.raises(ValueError, match="completed source receipt"):
        prepare_qwen_sandbox_home(cache, {}, assets=tmp_path / "assets")


def test_resume_uses_read_only_source_receipt(qwen_source: Path) -> None:
    run_root = qwen_source.parent / ".gobby/run/sandbox"
    first = prepare_qwen_sandbox_home(
        run_root / "first/cache", {}, assets=run_root / "first/assets"
    )
    # Writable run files cannot redirect the next launch's host configuration.
    (first.home / "qwen-home-source.json").write_text('{"source": "/unexpected"}')

    resumed = prepare_qwen_sandbox_home(
        run_root / "second/cache",
        {"QWEN_HOME": str(first.home)},
        assets=run_root / "second/assets",
    )

    assert resumed.source == qwen_source
    assert resumed.launch_env["QWEN_RUNTIME_DIR"] == str(qwen_source)
    assert (resumed.home / "oauth_creds.json").resolve() == qwen_source / "oauth_creds.json"


async def _launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> tuple[SandboxLaunch, dict[str, list[str]]]:
    monkeypatch.setattr(sys, "platform", "darwin")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    executable = tmp_path / "qwen"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.setattr(srt_runtime, "_resolve_provider_executable", lambda *_: str(executable))

    def installation(**_context: str | None) -> SrtInstallation:
        return SrtInstallation(tmp_path, executable, executable, executable)

    async def preflight(launch: SandboxLaunch, cwd: str, env: dict[str, str]) -> None:
        assert env["QWEN_HOME"] == launch.provider_env["QWEN_HOME"]

    monkeypatch.setattr(srt_runtime, "verify_srt_installation", installation)
    monkeypatch.setattr(srt_runtime, "_preflight_srt", preflight)
    launch = await prepare_sandbox_launch(
        config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
        provider="qwen",
        workspace_path=str(workspace),
        run_id="qwen-test",
        resolver=None,
        daemon_port=60887,
        websocket_port=60888,
        api_base=None,
        env={"PATH": "", **env},
        prewarm_pre_commit_store=False,
    )
    policy: dict[str, list[str]] = json.loads(Path(launch.policy_path or "").read_text())[
        "filesystem"
    ]
    return launch, policy


async def test_launch_uses_private_extension_store_and_protects_host_controls(
    qwen_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch, policy = await _launch(tmp_path, monkeypatch, {})

    private = Path(launch.provider_env["QWEN_HOME"])
    assert private != qwen_source
    assert launch.provider_env["QWEN_RUNTIME_DIR"] == str(qwen_source)
    for name in ("extensions", "extension-store", "settings.json"):
        assert str(qwen_source / name) in policy["denyWrite"]
    for name in ("settings.json", "skills", "oauth_creds.json"):
        assert str(private / name) in policy["denyWrite"]
    for name in ("extensions", "extension-store"):
        assert str(private / name) not in policy["denyWrite"]


async def test_relaunch_with_the_run_home_keeps_its_extension_store_writable(
    qwen_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = await _launch(tmp_path, monkeypatch, {})
    private = Path(first.provider_env["QWEN_HOME"])

    again, policy = await _launch(tmp_path, monkeypatch, {"QWEN_HOME": str(private)})

    assert again.provider_env["QWEN_HOME"] == str(private)
    assert str(qwen_source / "extension-store") in policy["denyWrite"]
    assert str(private / "extension-store") not in policy["denyWrite"]


async def test_operator_qwen_home_is_the_protected_source(
    qwen_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom-qwen"
    custom.mkdir()
    (custom / "settings.json").write_text('{"synthetic": "custom"}')

    launch, policy = await _launch(tmp_path, monkeypatch, {"QWEN_HOME": str(custom)})

    private = Path(launch.provider_env["QWEN_HOME"])
    assert private != custom
    assert (private / "settings.json").read_text() == '{"synthetic": "custom"}'
    # No grant reaches the operator's home, so default-deny protects its controls.
    assert not any(custom.is_relative_to(grant) for grant in map(Path, policy["allowWrite"]))
    assert str(private / "extension-store") not in policy["denyWrite"]
