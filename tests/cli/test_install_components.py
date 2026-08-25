"""Tests for the install component registry and runners."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest

from gobby.cli import install_components as components
from gobby.cli.install_components import (
    CLI_COMPONENTS,
    COMPONENTS,
    UNINSTALLABLE_COMPONENTS,
    EmbeddingOverrides,
    require_installed,
    run_install_components,
    run_uninstall_components,
)
from gobby.cli.install_setup_impeccable import ImpeccableRemovalResult
from gobby.cli.install_setup_rtk import RtkCleanupReport, RtkInstallStatus
from gobby.cli.installers import (
    install_claude,
    install_embedding,
    uninstall_claude,
    uninstall_qwen,
)


def _rtk_status(**overrides: Any) -> RtkInstallStatus:
    values: dict[str, Any] = {
        "binary_path": Path("/tmp/rtk"),
        "version": "1.2.3",
        "rule_enabled": True,
        "direct_artifact_conflicts": (),
        "health": "healthy",
        "managed_binary": True,
    }
    values.update(overrides)
    return RtkInstallStatus(**values)


@pytest.fixture
def runtime() -> MagicMock:
    fake = MagicMock(name="runtime")
    fake.require_config.return_value.hooks.provider_timeout = 150
    fake.require_database.return_value = MagicMock(name="db")
    return fake


class TestRegistry:
    def test_components_are_complete_and_distinct(self) -> None:
        assert len(COMPONENTS) == 12
        assert len(set(COMPONENTS)) == len(COMPONENTS)
        assert CLI_COMPONENTS <= set(COMPONENTS)
        assert set(UNINSTALLABLE_COMPONENTS) <= set(COMPONENTS)
        assert set(COMPONENTS) - set(UNINSTALLABLE_COMPONENTS) == {
            "voice",
            "embedding",
            "ide-settings",
        }

    def test_embedding_overrides_any_set(self) -> None:
        assert not EmbeddingOverrides().any_set
        assert EmbeddingOverrides(dim=3).any_set


class TestRequireInstalled:
    def test_passes_when_bootstrap_and_gdaemon_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        (tmp_path / "bootstrap.yaml").write_text("datastore_mode: local\n")
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "gdaemon").write_text("")

        outcome = "accepted"
        try:
            require_installed()
        except click.UsageError as exc:
            outcome = f"refused: {exc}"

        assert outcome == "accepted"

    def test_rejects_missing_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "gdaemon").write_text("")

        with pytest.raises(click.UsageError, match="run `gobby install` first"):
            require_installed()

    def test_rejects_missing_gdaemon(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        (tmp_path / "bootstrap.yaml").write_text("datastore_mode: local\n")

        with pytest.raises(click.UsageError, match="run `gobby install` first"):
            require_installed()


class TestRunInstallComponents:
    def test_cli_component_runs_global_install_with_provider_timeout(
        self, tmp_path: Path, runtime: MagicMock
    ) -> None:
        def fake_install(
            cli_name: str,
            installer: Any,
            project_path: Path,
            results: dict[str, dict[str, Any]],
            *,
            hook_timeout_seconds: int,
        ) -> None:
            results[cli_name] = {"success": True, "timeout": hook_timeout_seconds}

        with patch.object(
            components, "_run_standard_cli_install", side_effect=fake_install
        ) as run_install:
            results = run_install_components(
                ["claude"],
                project_path=tmp_path,
                no_interactive=False,
                embedding=None,
                runtime=runtime,
            )

        run_install.assert_called_once_with(
            "claude",
            install_claude,
            tmp_path,
            results,
            hook_timeout_seconds=150,
        )
        assert results == {"claude": {"success": True, "timeout": 150}}

    def test_components_run_in_the_order_given(self, tmp_path: Path, runtime: MagicMock) -> None:
        order: list[str] = []
        (tmp_path / ".git").mkdir()

        def rtk_step(*args: Any, **kwargs: Any) -> RtkInstallStatus:
            order.append("rtk")
            return _rtk_status()

        def cli_install(
            cli_name: str,
            installer: Any,
            project_path: Path,
            results: dict[str, dict[str, Any]],
            *,
            hook_timeout_seconds: int,
        ) -> None:
            order.append(cli_name)
            results[cli_name] = {"success": True}

        def git_hooks(installer: Any, project_path: Path, results: dict[str, Any]) -> None:
            order.append("git-hooks")
            results["git-hooks"] = {"success": True}

        with (
            patch.object(components, "reconcile_rtk_step", side_effect=rtk_step) as rtk,
            patch.object(components, "_run_standard_cli_install", side_effect=cli_install),
            patch.object(components, "_run_git_hooks_install", side_effect=git_hooks),
        ):
            results = run_install_components(
                ["rtk", "qwen", "git-hooks"],
                project_path=tmp_path,
                no_interactive=True,
                embedding=None,
                runtime=runtime,
            )

        assert order == ["rtk", "qwen", "git-hooks"]
        assert set(results) == {"rtk", "qwen", "git-hooks"}
        rtk.assert_called_once_with(
            runtime.require_database.return_value, True, no_interactive=True
        )

    def test_git_hooks_component_requires_a_repository(
        self, tmp_path: Path, runtime: MagicMock
    ) -> None:
        with pytest.raises(click.UsageError, match="not a git repository"):
            run_install_components(
                ["git-hooks"],
                project_path=tmp_path,
                no_interactive=True,
                embedding=None,
                runtime=runtime,
            )

    def test_rtk_component_records_status(self, tmp_path: Path, runtime: MagicMock) -> None:
        status = _rtk_status(binary_path=Path("/opt/rtk"), health="healthy")
        with patch.object(components, "reconcile_rtk_step", return_value=status):
            results = run_install_components(
                ["rtk"],
                project_path=tmp_path,
                no_interactive=False,
                embedding=None,
                runtime=runtime,
            )

        assert results == {
            "rtk": {
                "success": True,
                "health": "healthy",
                "rule_enabled": True,
                "binary_path": "/opt/rtk",
            }
        }

    def test_impeccable_component_records_provisioned_runtime(
        self, tmp_path: Path, runtime: MagicMock
    ) -> None:
        provisioned = MagicMock(path=tmp_path / "impeccable", version="3.5.0")
        with patch.object(
            components, "provision_impeccable", return_value=provisioned
        ) as provision:
            results = run_install_components(
                ["impeccable"],
                project_path=tmp_path,
                no_interactive=False,
                embedding=None,
                runtime=runtime,
            )

        provision.assert_called_once_with(tmp_path)
        assert results == {
            "impeccable": {
                "success": True,
                "path": str(tmp_path / "impeccable"),
                "version": "3.5.0",
            }
        }

    def test_voice_component_enables_voice_without_prompting(
        self, tmp_path: Path, runtime: MagicMock
    ) -> None:
        def fake_voice(results: dict[str, dict[str, Any]], **kwargs: Any) -> None:
            results["voice"] = {"success": True, "enabled": kwargs["voice_flag"]}

        with patch.object(components, "_run_voice_install", side_effect=fake_voice) as voice:
            results = run_install_components(
                ["voice"],
                project_path=tmp_path,
                no_interactive=True,
                embedding=None,
                runtime=runtime,
            )

        voice.assert_called_once_with(
            results,
            voice_flag=True,
            no_interactive=True,
            db=runtime.require_database.return_value,
        )
        assert results == {"voice": {"success": True, "enabled": True}}

    def test_embedding_component_forwards_overrides(
        self, tmp_path: Path, runtime: MagicMock
    ) -> None:
        overrides = EmbeddingOverrides(
            url="http://embed.local", provider="openai", model="text-3", dim=64
        )

        def fake_embedding(
            installer: Any, results: dict[str, dict[str, Any]], **kwargs: Any
        ) -> None:
            results["embedding"] = {"success": True, "model": kwargs["model_override"]}

        with patch.object(
            components, "_run_embedding_install", side_effect=fake_embedding
        ) as embedding:
            results = run_install_components(
                ["embedding"],
                project_path=tmp_path,
                no_interactive=True,
                embedding=overrides,
                runtime=runtime,
            )

        assert results == {"embedding": {"success": True, "model": "text-3"}}
        embedding.assert_called_once_with(
            install_embedding,
            results,
            no_interactive=True,
            api_base_override="http://embed.local",
            model_override="text-3",
            dim_override=64,
            provider_override="openai",
        )

    def test_ide_settings_component_configures_terminals(
        self, tmp_path: Path, runtime: MagicMock
    ) -> None:
        with patch.object(components, "configure_ide_terminals") as configure:
            results = run_install_components(
                ["ide-settings"],
                project_path=tmp_path,
                no_interactive=False,
                embedding=None,
                runtime=runtime,
            )

        configure.assert_called_once_with()
        assert results == {"ide-settings": {"success": True}}

    def test_unknown_component_is_a_usage_error(self, tmp_path: Path, runtime: MagicMock) -> None:
        with pytest.raises(click.UsageError, match="Unknown component: nope"):
            run_install_components(
                ["nope"],
                project_path=tmp_path,
                no_interactive=False,
                embedding=None,
                runtime=runtime,
            )


class TestRunUninstallComponents:
    def test_cli_components_uninstall_from_home_with_global_mode_where_needed(
        self, tmp_path: Path, runtime: MagicMock
    ) -> None:
        def fake_uninstall(
            cli_name: str,
            uninstaller: Any,
            uninstall_base: Path,
            results: dict[str, dict[str, Any]],
            **kwargs: Any,
        ) -> None:
            results[cli_name] = {"success": True, "kwargs": kwargs}

        with patch.object(
            components, "_run_standard_cli_uninstall", side_effect=fake_uninstall
        ) as run_uninstall:
            results = run_uninstall_components(
                ["claude", "qwen"], project_path=tmp_path, runtime=runtime
            )

        assert [call.args[:3] for call in run_uninstall.call_args_list] == [
            ("claude", uninstall_claude, Path.home()),
            ("qwen", uninstall_qwen, Path.home()),
        ]
        assert results == {
            "claude": {"success": True, "kwargs": {}},
            "qwen": {"success": True, "kwargs": {"mode": "global"}},
        }

    def test_git_hooks_component_removes_hooks_for_the_repo(
        self, tmp_path: Path, runtime: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        removed = {"success": True, "removed": ["pre-push"], "not_found": [], "error": None}
        with patch.object(components, "uninstall_git_hooks", return_value=removed) as uninstall:
            results = run_uninstall_components(
                ["git-hooks"], project_path=tmp_path, runtime=runtime
            )

        uninstall.assert_called_once_with(tmp_path)
        assert results == {"git-hooks": removed}
        assert "Removed git hook section: pre-push" in capsys.readouterr().out

    def test_git_hooks_component_reports_failure(
        self, tmp_path: Path, runtime: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        failed = {"success": False, "removed": [], "not_found": [], "error": "Not a git repository"}
        with patch.object(components, "uninstall_git_hooks", return_value=failed):
            results = run_uninstall_components(
                ["git-hooks"], project_path=tmp_path, runtime=runtime
            )

        assert results["git-hooks"]["success"] is False
        assert "Failed: Not a git repository" in capsys.readouterr().err

    def test_rtk_component_disables_rule_and_removes_binary(
        self, tmp_path: Path, runtime: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = RtkCleanupReport(
            removed=(tmp_path / "rtk",), backups=(), conflicts=("ambiguous alias kept",)
        )
        with (
            patch.object(components, "disable_rule_if_present", return_value=True) as disable,
            patch.object(components, "remove_managed_rtk", return_value=report) as remove,
        ):
            results = run_uninstall_components(["rtk"], project_path=tmp_path, runtime=runtime)

        disable.assert_called_once_with(runtime.require_database.return_value)
        remove.assert_called_once_with()
        assert results == {
            "rtk": {
                "success": True,
                "rule_disabled": True,
                "removed": [str(tmp_path / "rtk")],
                "conflicts": ["ambiguous alias kept"],
            }
        }
        captured = capsys.readouterr()
        assert f"Removed managed artifact: {tmp_path / 'rtk'}" in captured.out
        assert "Warning: ambiguous alias kept" in captured.err

    def test_impeccable_component_removes_runtime(
        self, tmp_path: Path, runtime: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        removal = ImpeccableRemovalResult(
            removed=(tmp_path / "impeccable",), skipped=("kept user cache",)
        )
        with patch.object(components, "remove_impeccable_runtime", return_value=removal):
            results = run_uninstall_components(
                ["impeccable"], project_path=tmp_path, runtime=runtime
            )

        assert results == {
            "impeccable": {
                "success": True,
                "removed": [str(tmp_path / "impeccable")],
                "skipped": ["kept user cache"],
            }
        }
        captured = capsys.readouterr()
        assert f"Removed managed artifact: {tmp_path / 'impeccable'}" in captured.out
        assert "Warning: kept user cache" in captured.err

    def test_config_only_components_cannot_be_uninstalled(
        self, tmp_path: Path, runtime: MagicMock
    ) -> None:
        with pytest.raises(click.UsageError, match="cannot be uninstalled: voice"):
            run_uninstall_components(["voice"], project_path=tmp_path, runtime=runtime)
