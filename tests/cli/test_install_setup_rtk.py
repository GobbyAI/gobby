from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.adapters.capabilities import get_provider_capabilities
from gobby.cli import install_setup_rtk
from gobby.cli.install_setup_rtk import (
    RtkCleanupReport,
    RtkInstallStatus,
    disable_rule_if_present,
    ensure_rtk,
    reconcile_direct_artifacts,
    reconcile_rtk,
    remove_managed_rtk,
    resolve_selection,
    set_rule_state,
)
from gobby.cli.uninstall import uninstall
from gobby.hooks.events import SessionSource
from gobby.integrations.rtk import (
    RTK_RULE_NAME,
    RtkProbe,
    platform_paths,
    probe_rtk,
    resolve_rtk,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent

pytestmark = pytest.mark.unit


def _write_fake_rtk(path: Path, *, version: str = "0.45.0", valid_contract: bool = True) -> None:
    help_text = "Command to check\\\\n  --agent <AGENT>" if valid_contract else "other help"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""#!/usr/bin/env python3
import sys
if sys.argv[1:] == ["--version"]:
    print("rtk {version}")
    raise SystemExit(0)
if sys.argv[1:] == ["hook", "check", "--help"]:
    print("{help_text}")
    raise SystemExit(0)
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _ensure_rule(db: HubDatabase, *, enabled: bool = False) -> None:
    manager = RuleDefinitionManager(db)
    row = manager.get_by_name(RTK_RULE_NAME, project_id=None)
    if row is not None:
        manager.update(row.id, enabled=enabled)
        return
    body = RuleDefinitionBody(
        event=RuleTriggerEvent.BEFORE_TOOL,
        effects=[RuleEffect(type="proxy_hook", handler="rtk")],
    )
    manager.create(
        name=RTK_RULE_NAME,
        definition_json=body.model_dump_json(),
        priority=90,
        enabled=enabled,
    )


def test_probe_accepts_minimum_hook_check_contract(tmp_path: Path) -> None:
    binary = tmp_path / "rtk"
    _write_fake_rtk(binary)

    result = probe_rtk(binary)

    assert result.compatible is True
    assert result.version == "0.45.0"
    assert result.path == binary.resolve()


@pytest.mark.parametrize(
    ("version", "valid_contract", "error"),
    [
        ("0.44.9", True, "0.45.0 or newer"),
        ("0.45.0-beta.1", True, "0.45.0 or newer"),
        ("0.45.0", False, "hook-check contract"),
    ],
)
def test_probe_rejects_incompatible_rtk(
    version: str,
    valid_contract: bool,
    error: str,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "rtk"
    _write_fake_rtk(binary, version=version, valid_contract=valid_contract)

    result = probe_rtk(binary)

    assert result.compatible is False
    assert error in (result.error or "")


def test_resolve_ignores_wrong_package_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision = tmp_path / "rtk"
    collision.write_text("#!/bin/sh\necho wrong-package\n", encoding="utf-8")
    collision.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("GOBBY_RTK_BIN", raising=False)

    assert resolve_rtk(home=tmp_path / "home") is None


def test_platform_paths_match_upstream_directory_rules(tmp_path: Path) -> None:
    linux = platform_paths(home=tmp_path, platform="linux", env={})
    mac = platform_paths(home=tmp_path, platform="darwin", env={})

    assert linux.config_dir == tmp_path / ".config" / "rtk"
    assert linux.data_dir == tmp_path / ".local" / "share" / "rtk"
    assert mac.config_dir == tmp_path / "Library" / "Application Support" / "rtk"
    assert mac.data_dir == mac.config_dir


def test_platform_paths_honor_config_and_environment(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "rtk"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[tracking]\ndatabase_path = "/custom/history.db"\n[tee]\ndirectory = "/custom/tee"\n',
        encoding="utf-8",
    )

    paths = platform_paths(
        home=tmp_path,
        platform="linux",
        env={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "RTK_DB_PATH": str(tmp_path / "override.db"),
        },
    )

    assert paths.database_path == tmp_path / "override.db"
    assert paths.tee_dir == Path("/custom/tee")


def test_homebrew_provisioning_uses_verified_formula_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brewed = RtkProbe(tmp_path / "brew" / "bin" / "rtk", "0.45.0", True)
    calls: list[list[str]] = []

    monkeypatch.setattr(install_setup_rtk, "resolve_rtk", lambda **kwargs: None)
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: "/brew")
    monkeypatch.setattr(install_setup_rtk, "_brew_probe", lambda brew: brewed)

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    assert ensure_rtk(home=tmp_path) == brewed
    assert calls == [["/brew", "install", "rtk"]]


def test_verified_fallback_writes_gobby_ownership_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = b"verified archive"
    checksum = hashlib.sha256(archive).hexdigest()
    binary = tmp_path / ".gobby" / "bin" / "rtk"

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit > len(archive)
            return archive

    def extract(*args: object, **kwargs: Any) -> bool:
        del args, kwargs
        _write_fake_rtk(binary)
        return True

    monkeypatch.setattr(
        install_setup_rtk,
        "_asset_for_platform",
        lambda: ("asset.tar.gz", ".tar.gz", "rtk"),
    )
    monkeypatch.setitem(install_setup_rtk._ASSET_CHECKSUMS, "asset.tar.gz", checksum)
    monkeypatch.setattr(install_setup_rtk, "_urlopen_https", lambda *args, **kwargs: Response())
    monkeypatch.setattr(install_setup_rtk, "_extract_binary_from_release_archive", extract)

    result = install_setup_rtk._download_fallback(home=tmp_path)

    assert result.compatible is True
    metadata = json.loads((binary.parent / ".rtk-gobby-install.json").read_text())
    assert metadata["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("explicit", "no_interactive", "current", "expected", "prompted"),
    [
        (True, True, None, True, False),
        (False, False, True, False, False),
        (None, True, None, False, False),
        (None, True, True, True, False),
        (None, False, True, True, True),
    ],
)
def test_tri_state_selection(
    explicit: bool | None,
    no_interactive: bool,
    current: bool | None,
    expected: bool,
    prompted: bool,
) -> None:
    calls: list[bool] = []

    def confirm(prompt: str, *, default: bool) -> bool:
        del prompt
        calls.append(default)
        return default

    assert (
        resolve_selection(
            explicit,
            no_interactive=no_interactive,
            current=current,
            confirm=confirm,
        )
        is expected
    )
    assert bool(calls) is prompted


def test_installed_rule_toggles_and_global_disable(temp_db: HubDatabase) -> None:
    _ensure_rule(temp_db, enabled=False)
    manager = RuleDefinitionManager(temp_db)

    assert set_rule_state(temp_db, enabled=True) is True
    enabled_rule = manager.get_by_name(RTK_RULE_NAME)
    assert enabled_rule is not None
    assert enabled_rule.enabled is True
    assert disable_rule_if_present(temp_db) is True
    disabled_rule = manager.get_by_name(RTK_RULE_NAME)
    assert disabled_rule is not None
    assert disabled_rule.enabled is False


def test_reconcile_enables_rule_after_cleanup(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_rule(temp_db, enabled=False)
    probe = RtkProbe(tmp_path / "rtk", "0.45.0", True)
    expected = RtkInstallStatus(probe.path, probe.version, True, (), "healthy", False)
    monkeypatch.setattr(install_setup_rtk, "ensure_rtk", lambda **kwargs: probe)
    monkeypatch.setattr(
        install_setup_rtk,
        "reconcile_direct_artifacts",
        lambda **kwargs: RtkCleanupReport((), (), ()),
    )
    monkeypatch.setattr(install_setup_rtk, "get_rtk_status", lambda *args, **kwargs: expected)

    result = reconcile_rtk(
        temp_db,
        True,
        no_interactive=True,
        confirm=lambda *args, **kwargs: False,
        home=tmp_path,
    )

    assert result == expected
    rule = RuleDefinitionManager(temp_db).get_by_name(RTK_RULE_NAME)
    assert rule is not None
    assert rule.enabled is True


def test_exact_hook_cleanup_backs_up_and_preserves_unrelated_content(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": "rtk hook claude"},
                                {"type": "command", "command": "echo keep"},
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    instructions = settings.parent / "CLAUDE.md"
    instructions.write_text(
        "# Keep\n\n<!-- rtk-instructions v2 -->\nremove me\n"
        "<!-- /rtk-instructions -->\n\n# Also keep\n",
        encoding="utf-8",
    )

    report = reconcile_direct_artifacts(home=tmp_path, remove=True)

    parsed = json.loads(settings.read_text())
    assert parsed["theme"] == "dark"
    assert parsed["hooks"]["PreToolUse"][0]["hooks"] == [
        {"type": "command", "command": "echo keep"}
    ]
    assert instructions.read_text() == "# Keep\n\n\n# Also keep\n"
    assert settings in report.removed
    assert instructions in report.removed
    assert len(report.backups) == 2
    assert report.conflicts == ()


def test_ambiguous_artifacts_are_preserved_and_reported(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = {"hooks": [{"command": "rtk hook claude --custom"}]}
    settings.write_text(json.dumps(original), encoding="utf-8")
    instructions = settings.parent / "CLAUDE.md"
    original_instructions = (
        "<!-- rtk-instructions locally-modified -->\nkeep me\n<!-- /rtk-instructions -->\n"
    )
    instructions.write_text(original_instructions, encoding="utf-8")

    report = reconcile_direct_artifacts(home=tmp_path, remove=True)

    assert json.loads(settings.read_text()) == original
    assert instructions.read_text() == original_instructions
    assert report.removed == ()
    assert "ambiguous RTK hook command" in report.conflicts[0]
    assert any("malformed RTK instruction block preserved" in item for item in report.conflicts)


def test_remove_managed_rtk_preserves_modified_binary(tmp_path: Path) -> None:
    binary = tmp_path / ".gobby" / "bin" / "rtk"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"modified")
    (binary.parent / ".rtk-gobby-install.json").write_text(
        json.dumps(
            {
                "path": str(binary),
                "sha256": hashlib.sha256(b"original").hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    report = remove_managed_rtk(home=tmp_path)

    assert binary.exists()
    assert "was modified" in report.conflicts[0]


def test_remove_managed_rtk_preserves_path_mismatch(tmp_path: Path) -> None:
    binary = tmp_path / ".gobby" / "bin" / "rtk"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"owned")
    sidecar = binary.parent / ".rtk-gobby-install.json"
    sidecar.write_text(
        json.dumps(
            {
                "path": str(tmp_path / "different" / "rtk"),
                "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    report = remove_managed_rtk(home=tmp_path)

    assert binary.exists()
    assert sidecar.exists()
    assert "path does not match" in report.conflicts[0]


def test_global_uninstall_tools_disables_rule_and_removes_owned_fallback(tmp_path: Path) -> None:
    binary = tmp_path / ".gobby" / "bin" / "rtk"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"owned")
    sidecar = binary.parent / ".rtk-gobby-install.json"
    sidecar.write_text(
        json.dumps(
            {
                "path": str(binary),
                "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "version": "0.45.0",
            }
        ),
        encoding="utf-8",
    )
    database = MagicMock()
    runtime = MagicMock()
    runtime.require_database.return_value = database
    impeccable_cleanup = MagicMock(removed=(), skipped=())

    with (
        patch("gobby.cli.uninstall.Path.home", return_value=tmp_path),
        patch("gobby.cli.uninstall.get_cli_runtime", return_value=runtime),
        patch("gobby.cli.uninstall.disable_rule_if_present") as disable,
        patch(
            "gobby.cli.uninstall.remove_impeccable_runtime",
            return_value=impeccable_cleanup,
        ),
    ):
        result = CliRunner().invoke(uninstall, ["--tools", "--yes"])

    assert result.exit_code == 0
    disable.assert_called_once_with(database)
    runtime.close.assert_called_once_with()
    assert not binary.exists()
    assert not sidecar.exists()


def test_supported_sources_match_v1_adapter_matrix() -> None:
    supported = {
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.QWEN,
        SessionSource.GROK,
        SessionSource.DROID,
        SessionSource.AGY,
    }

    assert all(
        get_provider_capabilities(source).supports_permission_neutral_rewrite
        for source in supported
    )
    assert not get_provider_capabilities(SessionSource.UNKNOWN).supports_permission_neutral_rewrite
