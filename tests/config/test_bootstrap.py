"""Bootstrap configuration tests."""

from pathlib import Path

import pytest
import yaml

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


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("daemon_port", "not-a-number"),
        ("daemon_port", True),
        ("websocket_port", 60888.5),
        ("ui_port", [60889]),
        ("bind_host", ["localhost"]),
    ],
)
def test_bootstrap_rejects_malformed_scalar_values(
    tmp_path: Path, field_name: str, value: object
) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, yaml.safe_dump({field_name: value}))

    with pytest.raises(BootstrapConfigError, match=field_name):
        load_bootstrap(str(bootstrap_path))


def test_bootstrap_preserves_valid_explicit_scalar_values(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_path,
        yaml.safe_dump(
            {
                "daemon_port": 61234,
                "bind_host": "127.0.0.1",
                "websocket_port": 61235,
                "ui_port": 61236,
                "hub_backend": "postgres",
                "database_url": "postgresql://gobby:secret@localhost/gobby",
            }
        ),
    )

    bootstrap = load_bootstrap(str(bootstrap_path), resolve_database_url=True)

    assert bootstrap.daemon_port == 61234
    assert bootstrap.bind_host == "127.0.0.1"
    assert bootstrap.websocket_port == 61235
    assert bootstrap.ui_port == 61236
    assert bootstrap.database_url == "postgresql://gobby:secret@localhost/gobby"
