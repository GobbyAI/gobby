"""Bootstrap config tests for removed FalkorDB password settings."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _write_bootstrap(path: Path, content: str) -> None:
    if (
        "files_home:" not in content
        and "hub_daemon_url:" not in content
        and "datastore_mode: remote" not in content
    ):
        files_home = path.parent / "files"
        files_home.mkdir(exist_ok=True)
        content = f"{content}files_home: {files_home}\n"
    elif "datastore_mode: remote" in content and "hub_daemon_url:" not in content:
        content = f"{content}hub_daemon_url: http://hub.example.test:60887\n"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def test_bootstrap_config_excludes_falkordb_password_field() -> None:
    from gobby.config.bootstrap import BootstrapConfig

    field_names = {field.name for field in fields(BootstrapConfig)}

    assert "falkordb_password" not in field_names
    assert "neo4j_password" not in field_names


def test_load_bootstrap_ignores_falkordb_password_yaml(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(bootstrap_file, "falkordb_password: custom-secret\n")

    bootstrap = load_bootstrap(str(bootstrap_file))

    assert not hasattr(bootstrap, "falkordb_password")


def test_load_bootstrap_ignores_falkordb_password_environment(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.config.bootstrap import load_bootstrap

    monkeypatch.setenv("GOBBY_FALKORDB_PASSWORD", "env-secret")

    bootstrap = load_bootstrap(str(temp_dir / "missing.yaml"))

    assert not hasattr(bootstrap, "falkordb_password")
