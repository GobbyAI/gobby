"""Bootstrap config tests for FalkorDB password settings."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _write_bootstrap(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def test_bootstrap_config_uses_falkordb_password_field() -> None:
    from gobby.config.bootstrap import BootstrapConfig

    field_names = {field.name for field in fields(BootstrapConfig)}

    assert "falkordb_password" in field_names
    assert "neo4j_password" not in field_names
    assert BootstrapConfig().falkordb_password == "gobbyfalkor"


def test_load_bootstrap_reads_falkordb_password_from_yaml(temp_dir: Path) -> None:
    from gobby.config.bootstrap import load_bootstrap

    bootstrap_file = temp_dir / "bootstrap.yaml"
    _write_bootstrap(bootstrap_file, "falkordb_password: custom-secret\n")

    bootstrap = load_bootstrap(str(bootstrap_file))

    assert bootstrap.falkordb_password == "custom-secret"
    assert not hasattr(bootstrap, "neo4j_password")


def test_load_bootstrap_reads_falkordb_password_from_environment(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.config.bootstrap import load_bootstrap

    monkeypatch.setenv("GOBBY_FALKORDB_PASSWORD", "env-secret")

    bootstrap = load_bootstrap(str(temp_dir / "missing.yaml"))

    assert bootstrap.falkordb_password == "env-secret"
