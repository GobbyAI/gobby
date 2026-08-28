"""Tests for gterm and gclient managed-binary installers."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.cli.install_setup import (
    _STAGE0_TARGETS,
    MANAGED_NATIVE_BINARY_NAMES,
    _install_gclient,
    _install_gclient_from_github,
    _install_gclient_from_submodule,
    _install_gterm,
    _install_gterm_from_github,
    _install_gterm_from_submodule,
)
from gobby.cli.install_setup_gterm import GTERM_NO_ZIG_SKIP_REASON
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit
GTERM_PIN = MANAGED_BIN_VERSION_PINS["gterm"]
GCLIENT_PIN = MANAGED_BIN_VERSION_PINS["gclient"]
STAGE0_TRIPLES = (
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
)


def _which_cargo_only(name: str) -> str | None:
    return "/usr/bin/cargo" if name == "cargo" else None


class TestGtermInstaller:
    def test_stage0_targets_are_macos_linux_only(self) -> None:
        assert tuple(sorted(_STAGE0_TARGETS.values())) == tuple(sorted(STAGE0_TRIPLES))
        assert not any("windows" in target for target in _STAGE0_TARGETS.values())

    @patch("gobby.cli.install_setup.sys.platform", "win32")
    @patch("gobby.cli.install_setup.platform.machine", return_value="amd64")
    def test_skips_windows(self, _mock_machine: MagicMock, tmp_path: Path) -> None:
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            result = _install_gterm()
        assert result["skipped"] is True
        assert "unsupported platform" in str(result["reason"])

    def test_workspace_build_skips_without_zig(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "crates" / "gterminal").mkdir(parents=True)
        (workspace / "src" / "gobby" / "cli").mkdir(parents=True)
        (workspace / "Cargo.toml").touch()
        (workspace / "crates" / "gterminal" / "Cargo.toml").touch()
        dest = tmp_path / "bin"
        dest.mkdir()

        with (
            patch("gobby.cli.install_setup.shutil.which", side_effect=_which_cargo_only),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
            patch(
                "gobby.cli.install_setup_gterm.__file__",
                str(workspace / "src" / "gobby" / "cli" / "install_setup_gterm.py"),
            ),
        ):
            result = _install_gterm_from_submodule(dest)

        assert result is False
        mock_run.assert_not_called()
        assert GTERM_NO_ZIG_SKIP_REASON in capsys.readouterr().out

    def test_workspace_build_uses_vt_engine_and_600s_timeout(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "crates" / "gterminal").mkdir(parents=True)
        (workspace / "src" / "gobby" / "cli").mkdir(parents=True)
        (workspace / "Cargo.toml").touch()
        (workspace / "crates" / "gterminal" / "Cargo.toml").touch()
        source = workspace / "target" / "release" / "gterm"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"gterm-bin")
        dest = tmp_path / "bin"
        dest.mkdir()
        lock = MagicMock()

        def fake_which(name: str) -> str | None:
            if name in {"cargo", "zig"}:
                return f"/usr/bin/{name}"
            return None

        with (
            patch("gobby.cli.install_setup.shutil.which", side_effect=fake_which),
            patch(
                "gobby.cli.install_setup.subprocess.run",
                return_value=MagicMock(returncode=0),
            ) as mock_run,
            patch(
                "gobby.cli.install_setup_gterm.__file__",
                str(workspace / "src" / "gobby" / "cli" / "install_setup_gterm.py"),
            ),
            patch(
                "gobby.cli.install_setup_gterm.try_acquire_native_bin_lock",
                return_value=lock,
            ),
            patch("gobby.install.bin_freshness_promotion.os.replace", wraps=os.replace),
        ):
            result = _install_gterm_from_submodule(dest)

        assert result is True
        command = mock_run.call_args.args[0]
        assert command[:6] == [
            "cargo",
            "build",
            "--release",
            "-p",
            "gobby-terminal",
            "--features",
        ]
        assert "vt-engine" in command
        assert mock_run.call_args.kwargs["timeout"] == 600
        assert (dest / "gterm").read_bytes() == b"gterm-bin"

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    def test_no_zig_local_build_continues_to_github(
        self, _mock_machine: MagicMock, tmp_path: Path
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "gterm").write_bytes(b"\x00")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_installed_gterm_version", return_value=None),
            patch(
                "gobby.cli.install_setup._install_gterm_from_submodule", return_value=False
            ) as mock_sub,
            patch(
                "gobby.cli.install_setup._install_gterm_from_github",
                return_value=True,
            ) as mock_github,
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
            patch("gobby.cli.install_setup_gterm.probe_gterm_version", return_value=GTERM_PIN),
        ):
            result = _install_gterm()

        mock_sub.assert_called_once()
        mock_github.assert_called_once_with(bin_dir, "aarch64-apple-darwin", GTERM_PIN)
        assert result["method"] == "github"
        assert result["version"] == GTERM_PIN

    def test_github_uses_gterm_tag_prefix(self, tmp_path: Path) -> None:
        with patch(
            "gobby.cli.install_setup._download_release_binary",
            return_value=True,
        ) as mock_download:
            assert _install_gterm_from_github(tmp_path, "aarch64-apple-darwin", "0.1.0") is True
        mock_download.assert_called_once()
        kwargs = mock_download.call_args.kwargs
        assert kwargs["artifact_name"] == "gterm"
        assert kwargs["tag_prefix"] == "gterm-v"
        assert kwargs["binary_name"] == "gterm"


class TestGclientInstaller:
    @patch("gobby.cli.install_setup.sys.platform", "win32")
    @patch("gobby.cli.install_setup.platform.machine", return_value="amd64")
    def test_skips_windows(self, _mock_machine: MagicMock, tmp_path: Path) -> None:
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            result = _install_gclient()
        assert result["skipped"] is True
        assert "unsupported platform" in str(result["reason"])

    def test_workspace_build_is_zig_free(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "crates" / "gclient").mkdir(parents=True)
        (workspace / "src" / "gobby" / "cli").mkdir(parents=True)
        (workspace / "Cargo.toml").touch()
        (workspace / "crates" / "gclient" / "Cargo.toml").touch()
        source = workspace / "target" / "release" / "gclient"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"gclient-bin")
        dest = tmp_path / "bin"
        dest.mkdir()
        lock = MagicMock()

        with (
            patch("gobby.cli.install_setup.shutil.which", side_effect=_which_cargo_only),
            patch(
                "gobby.cli.install_setup.subprocess.run",
                return_value=MagicMock(returncode=0),
            ) as mock_run,
            patch(
                "gobby.cli.install_setup_gclient.__file__",
                str(workspace / "src" / "gobby" / "cli" / "install_setup_gclient.py"),
            ),
            patch(
                "gobby.cli.install_setup_gclient.try_acquire_native_bin_lock",
                return_value=lock,
            ),
            patch("gobby.install.bin_freshness_promotion.os.replace", wraps=os.replace),
        ):
            result = _install_gclient_from_submodule(dest)

        assert result is True
        command = mock_run.call_args.args[0]
        assert "--features" not in command
        assert "vt-engine" not in command
        assert command[:5] == ["cargo", "build", "--release", "-p", "gobby-client"]
        assert mock_run.call_args.kwargs["timeout"] == 180
        assert (dest / "gclient").read_bytes() == b"gclient-bin"

    def test_github_uses_gclient_tag_prefix(self, tmp_path: Path) -> None:
        with patch(
            "gobby.cli.install_setup._download_release_binary",
            return_value=True,
        ) as mock_download:
            assert _install_gclient_from_github(tmp_path, "aarch64-apple-darwin", "0.1.0") is True
        kwargs = mock_download.call_args.kwargs
        assert kwargs["artifact_name"] == "gclient"
        assert kwargs["tag_prefix"] == "gclient-v"
        assert kwargs["binary_name"] == "gclient"


def test_managed_native_binary_install_inventory() -> None:
    assert MANAGED_NATIVE_BINARY_NAMES == ("gcode", "ghook", "gwiki", "gterm", "gclient")


def test_release_workflows_gate_stage0_and_gclient_preflight() -> None:
    root = Path(__file__).resolve().parents[2]
    gterm = yaml.safe_load((root / ".github" / "workflows" / "release-gterminal.yml").read_text())
    gclient = yaml.safe_load((root / ".github" / "workflows" / "release-gclient.yml").read_text())
    rust_ci = (root / ".github" / "workflows" / "rust-ci.yml").read_text()

    gterm_targets = [
        row["target"] for row in gterm["jobs"]["build"]["strategy"]["matrix"]["include"]
    ]
    gclient_targets = [
        row["target"] for row in gclient["jobs"]["build"]["strategy"]["matrix"]["include"]
    ]
    assert gterm_targets == list(STAGE0_TRIPLES)
    assert gclient_targets == list(STAGE0_TRIPLES)
    assert all("windows" not in target for target in gterm_targets + gclient_targets)

    gterm_yaml = (root / ".github" / "workflows" / "release-gterminal.yml").read_text()
    gclient_yaml = (root / ".github" / "workflows" / "release-gclient.yml").read_text()
    assert "mlugg/setup-zig" in gterm_yaml
    assert "0.15.2" in gterm_yaml
    assert "mlugg/setup-zig" not in gclient_yaml
    assert "cargo publish -p gobby-terminal --dry-run" in gterm_yaml
    assert "cargo publish -p gobby-client --dry-run" in gclient_yaml
    assert "python3 .github/scripts/require_gobby_terminal_on_crates_io.py" in gclient_yaml
    assert gclient_yaml.index("require_gobby_terminal_on_crates_io.py") < gclient_yaml.index(
        "cargo package -p gobby-client"
    )
    assert "cargo clippy -p gobby-client" in rust_ci
    assert "cargo nextest run --profile ci -p gobby-client" in rust_ci
