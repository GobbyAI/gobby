"""Contracts for fatal, pre-database gdaemon provisioning."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import click
import pytest

from gobby.cli import install_setup, install_setup_gdaemon
from gobby.install import bin_set_coherence
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.storage import schema_contract

_GDAEMON_PIN = MANAGED_BIN_VERSION_PINS["gdaemon"]


def _stub_non_schema_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from gobby import sync_registry
    from gobby.cli import install_setup_impeccable, install_setup_srt
    from gobby.cli.installers import ide_config, tmux_config

    def sync_bundled(*args: object, **kwargs: object) -> dict[str, object]:
        return {"total_synced": 0, "errors": []}

    monkeypatch.setattr(
        sync_registry,
        "sync_bundled_content_to_db",
        sync_bundled,
    )
    monkeypatch.setattr(
        install_setup_srt,
        "install_srt_runtime",
        lambda: SimpleNamespace(installed=False, version="test", path=tmp_path),
    )
    monkeypatch.setattr(
        install_setup_impeccable,
        "install_impeccable_cli",
        lambda: SimpleNamespace(installed=False, version="test", path=tmp_path),
    )
    monkeypatch.setattr(tmux_config, "configure_tmux_clipboard", lambda: {"success": True})
    monkeypatch.setattr(
        ide_config,
        "configure_vscode_family_terminal_integration",
        lambda: {},
    )
    monkeypatch.setattr(install_setup, "_run_npm_install", lambda *args: None)
    monkeypatch.setattr(install_setup, "_run_managed_native_binary_installs", lambda: None)
    monkeypatch.setattr(install_setup, "is_homebrew_distribution", lambda: False)


def test_run_daemon_setup_provisions_gdaemon_before_database_init(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from gobby.storage.hub import runtime

    events: list[str] = []

    def provision() -> dict[str, object]:
        events.append("gdaemon")
        return {"installed": False, "version": "0.1.0", "method": "existing"}

    monkeypatch.setattr(install_setup, "ensure_gdaemon", provision)

    @contextmanager
    def database() -> Iterator[MagicMock]:
        events.append("database")
        db = MagicMock()
        db.list_templates.return_value = []
        yield db

    monkeypatch.setattr(runtime, "runtime_hub_database", database)
    _stub_non_schema_setup(monkeypatch, tmp_path)

    install_setup.run_daemon_setup(tmp_path, configure_ide_settings=False)

    assert events[:2] == ["gdaemon", "database"]


def test_run_daemon_setup_fails_before_database_when_gdaemon_install_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = MagicMock()
    ensure = MagicMock(side_effect=install_setup_gdaemon.GdaemonInstallError("no usable binary"))
    monkeypatch.setattr(install_setup, "ensure_gdaemon", ensure)
    monkeypatch.setattr("gobby.storage.hub.runtime.runtime_hub_database", database)

    with pytest.raises(click.ClickException, match="no usable binary") as exc_info:
        install_setup.run_daemon_setup(tmp_path, configure_ide_settings=False)

    assert str(exc_info.value) == "Failed to provision gdaemon: no usable binary"
    ensure.assert_called_once_with()
    database.assert_not_called()


def test_run_daemon_setup_fails_when_initial_schema_apply_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = MagicMock()
    database.return_value.__enter__.side_effect = schema_contract.SchemaContractError(
        "gdaemon schema apply failed"
    )
    monkeypatch.setattr(
        install_setup,
        "ensure_gdaemon",
        lambda: {"installed": False, "version": "0.1.0", "method": "existing"},
    )
    monkeypatch.setattr("gobby.storage.hub.runtime.runtime_hub_database", database)

    with pytest.raises(click.ClickException, match="gdaemon schema apply failed"):
        install_setup.run_daemon_setup(tmp_path, configure_ide_settings=False)


def test_existing_gdaemon_is_accepted_only_with_exact_schema_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "gdaemon"
    binary.write_bytes(b"binary")
    expected = schema_contract.expected_schema_identity()
    monkeypatch.setattr(install_setup_gdaemon, "_probe_version", lambda path: _GDAEMON_PIN)
    monkeypatch.setattr(install_setup_gdaemon, "_probe_identity", lambda path: expected)

    result = install_setup_gdaemon.ensure_gdaemon(bin_dir=tmp_path)

    assert result == {"installed": False, "version": _GDAEMON_PIN, "method": "existing"}


def test_existing_gdaemon_with_stale_sidecar_and_wrong_binary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "gdaemon"
    binary.write_bytes(b"binary")
    (tmp_path / ".gdaemon-schema-identity.json").write_text(
        schema_contract.expected_schema_identity_json(),
        encoding="utf-8",
    )
    observed = {**schema_contract.expected_schema_identity(), "runner_protocol": 0}
    monkeypatch.setattr(install_setup_gdaemon, "_probe_version", lambda path: _GDAEMON_PIN)
    monkeypatch.setattr(install_setup_gdaemon, "_probe_identity", lambda path: observed)
    monkeypatch.setattr(install_setup_gdaemon, "_install_gdaemon", lambda *args: None)

    with pytest.raises(install_setup_gdaemon.GdaemonInstallError, match="identity mismatch"):
        install_setup_gdaemon.ensure_gdaemon(bin_dir=tmp_path)


def test_codesign_timeout_is_reported_as_install_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "gdaemon"
    binary.write_bytes(b"binary")
    monkeypatch.setattr(install_setup_gdaemon, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        install_setup_gdaemon,
        "shutil",
        SimpleNamespace(which=lambda command: "/usr/bin/codesign"),
    )

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("codesign", 30)

    monkeypatch.setattr(
        install_setup_gdaemon,
        "subprocess",
        SimpleNamespace(run=timeout, TimeoutExpired=subprocess.TimeoutExpired),
    )

    with pytest.raises(
        install_setup_gdaemon.GdaemonInstallError,
        match="gdaemon ad-hoc signing timed out",
    ):
        install_setup_gdaemon._codesign(binary)


def test_workspace_set_refusal_is_reported_as_install_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A mixed-set refusal surfaces as the install error the CLI already renders."""
    manifest = tmp_path / "workspace" / "Cargo.toml"
    source = manifest.parent / "target" / "release" / "gdaemon"
    source.parent.mkdir(parents=True)
    manifest.write_text("[workspace]\n", encoding="utf-8")
    built_identity = {**schema_contract.expected_schema_identity(), "latest_checksum": "f" * 64}
    source.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{json.dumps(built_identity)}'\n", encoding="utf-8"
    )
    source.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    installed = bin_dir / "gdaemon"
    installed.write_bytes(b"installed-gdaemon")
    (bin_dir / ".gdaemon-schema-identity.json").write_text(
        schema_contract.expected_schema_identity_json(), encoding="utf-8"
    )
    monkeypatch.setattr(install_setup_gdaemon, "_workspace_manifest", lambda: manifest)
    monkeypatch.setattr(
        install_setup_gdaemon,
        "shutil",
        SimpleNamespace(which=lambda command: "/usr/bin/cargo"),
    )
    monkeypatch.setattr(
        install_setup_gdaemon,
        "subprocess",
        SimpleNamespace(
            run=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )

    with pytest.raises(install_setup_gdaemon.GdaemonInstallError) as exc_info:
        install_setup_gdaemon._install_from_workspace(installed)

    message = str(exc_info.value)
    assert "workspace binary set promotion refused" in message
    assert "gdaemon embedded identity" in message
    assert "rebuild and install all four together" in message
    assert isinstance(exc_info.value.__cause__, bin_set_coherence.BinarySetCoherenceError)
    assert installed.read_bytes() == b"installed-gdaemon"
