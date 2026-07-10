"""Bootstrap configuration tests."""

from pathlib import Path

import pytest

from gobby.config.app import load_config
from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap


def _write_bootstrap(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o600)


def test_auth_mode_defaults_to_required(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"

    assert load_bootstrap(str(bootstrap_path)).auth_mode == "required"


@pytest.mark.parametrize("auth_mode", ["required", "disabled"])
def test_auth_mode_parsing(tmp_path: Path, auth_mode: str) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, f"auth_mode: {auth_mode}\n")

    assert load_bootstrap(str(bootstrap_path)).auth_mode == auth_mode


def test_auth_mode_rejects_unknown_value(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"

    _write_bootstrap(bootstrap_path, "auth_mode: optional\n")
    with pytest.raises(BootstrapConfigError, match="auth_mode"):
        load_bootstrap(str(bootstrap_path))


def test_auth_mode_flows_to_daemon_config(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, "auth_mode: disabled\n")

    config = load_config(config_file=str(bootstrap_path))

    assert config.auth_mode == "disabled"
