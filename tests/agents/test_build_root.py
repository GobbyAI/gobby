"""The agent build root: one configured, canonical directory holds every build folder."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from gobby.agents.build_root import agent_build_root, configure_build_root
from gobby.agents.cargo_target import (
    agent_checkout_cargo_target_dir,
    checkout_cargo_target_dir,
    cleanup_checkout_cargo_target_dir,
    ensure_agent_cargo_target_dir,
    sandbox_checkout_cargo_target_dir,
)
from gobby.agents.sandbox_policy import prepare_sandbox_run_paths, sandbox_cache_path
from gobby.config.registry import CONFIG_REGISTRY, ActivationPolicy

pytestmark = pytest.mark.unit

PROJECT = "project-1"


@pytest.fixture
def gobby_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    yield home
    configure_build_root(None)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "checkouts" / "task-1"
    path.mkdir(parents=True)
    (path / "Cargo.toml").write_text('[package]\nname = "x"\n', encoding="utf-8")
    return path


@pytest.fixture
def volume(tmp_path: Path) -> Path:
    path = tmp_path / "GobbyBuild"
    path.mkdir()
    return path


def test_default_root_is_the_gobby_cache(gobby_home: Path) -> None:
    assert agent_build_root() == gobby_home / "cache"


def test_configured_root_holds_both_trust_domains_targets(
    gobby_home: Path, checkout: Path, volume: Path
) -> None:
    assert configure_build_root(str(volume)) == volume

    unsandboxed = agent_checkout_cargo_target_dir(checkout, PROJECT)
    sandboxed = sandbox_checkout_cargo_target_dir(checkout)
    operator = checkout_cargo_target_dir(checkout, PROJECT)

    assert unsandboxed.is_relative_to(volume)
    assert sandboxed.is_relative_to(volume)
    assert operator.is_relative_to(volume)
    assert not sandboxed.is_relative_to(gobby_home / "cache")


def test_empty_value_restores_the_default_root(
    gobby_home: Path, checkout: Path, volume: Path
) -> None:
    configure_build_root(str(volume))
    assert configure_build_root(None) == gobby_home / "cache"
    assert sandbox_checkout_cargo_target_dir(checkout).is_relative_to(gobby_home / "cache")


def test_cleanup_removes_every_target_under_the_configured_root(
    gobby_home: Path, checkout: Path, volume: Path
) -> None:
    configure_build_root(str(volume))
    for target in (
        checkout_cargo_target_dir(checkout, PROJECT),
        agent_checkout_cargo_target_dir(checkout, PROJECT),
        sandbox_checkout_cargo_target_dir(checkout),
    ):
        target.mkdir(parents=True)
        (target / "artifact").write_text("x", encoding="utf-8")

    assert cleanup_checkout_cargo_target_dir(checkout, PROJECT) is None

    assert not checkout_cargo_target_dir(checkout, PROJECT).exists()
    assert not agent_checkout_cargo_target_dir(checkout, PROJECT).exists()
    assert not sandbox_checkout_cargo_target_dir(checkout).exists()


@pytest.mark.parametrize("value", ["relative/build", "./build"])
def test_relative_root_is_refused(gobby_home: Path, value: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        configure_build_root(value)


def test_missing_root_is_refused_so_an_unmounted_volume_never_fills_the_boot_disk(
    gobby_home: Path, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        configure_build_root(str(tmp_path / "GobbyBuild-unmounted"))


def test_symlinked_root_is_refused(gobby_home: Path, tmp_path: Path, volume: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(volume, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical"):
        configure_build_root(str(link))


def test_filesystem_root_and_home_directory_are_refused(gobby_home: Path) -> None:
    with pytest.raises(ValueError, match="too broad"):
        configure_build_root("/")
    with pytest.raises(ValueError, match="too broad"):
        configure_build_root(str(Path.home()))


def test_passwd_home_is_refused_even_when_home_env_points_elsewhere(
    gobby_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = tmp_path / "account"
    account.mkdir()
    monkeypatch.setattr("gobby.config.agent_build.account_home", lambda: account)
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    with pytest.raises(ValueError, match="too broad"):
        configure_build_root(str(account))


def test_sandbox_grant_accepts_the_target_under_the_configured_root(
    gobby_home: Path, checkout: Path, volume: Path
) -> None:
    configure_build_root(str(volume))
    target = sandbox_checkout_cargo_target_dir(checkout)

    assert sandbox_cache_path(target) == str(target)


def test_sandbox_grant_refuses_a_link_under_the_configured_root(
    gobby_home: Path, checkout: Path, volume: Path, tmp_path: Path
) -> None:
    configure_build_root(str(volume))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (volume / "sandbox").mkdir()
    (volume / "sandbox" / "cargo-target").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(PermissionError, match="sandbox cache"):
        sandbox_cache_path(sandbox_checkout_cargo_target_dir(checkout))


def test_agent_launch_refuses_a_configured_root_that_disappeared(
    gobby_home: Path, checkout: Path, volume: Path
) -> None:
    configure_build_root(str(volume))
    volume.rmdir()

    with pytest.raises(ValueError, match="does not exist"):
        ensure_agent_cargo_target_dir(checkout, PROJECT)

    assert not volume.exists()


def test_sandbox_launch_refuses_a_configured_root_that_disappeared(
    gobby_home: Path, checkout: Path, volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_build_root(str(volume))
    volume.rmdir()
    monkeypatch.setattr(
        "gobby.agents.sandbox_policy.secure_policy_directory",
        lambda run_id: gobby_home / "runs" / run_id,
    )

    with pytest.raises(ValueError, match="does not exist"):
        prepare_sandbox_run_paths(
            "missing-volume", {}, workspace=checkout, prewarm_pre_commit_store=False
        )

    assert not volume.exists()


def test_build_root_is_restart_required() -> None:
    spec = CONFIG_REGISTRY.resolve("agent_build.build_root")
    assert spec.activation is ActivationPolicy.RESTART_REQUIRED
