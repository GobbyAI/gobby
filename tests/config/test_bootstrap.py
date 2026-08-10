"""Bootstrap configuration tests."""

from pathlib import Path

import pytest
import yaml

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


def test_auth_mode_loads_from_bootstrap(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, "auth_mode: disabled\n")

    config = load_bootstrap(str(bootstrap_path))

    assert config.auth_mode == "disabled"


def test_datastore_mode_parsing(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"

    _write_bootstrap(bootstrap_path, "daemon_port: 60887\n")
    assert load_bootstrap(str(bootstrap_path)).datastore_mode == "local"

    for datastore_mode in ("local", "remote"):
        _write_bootstrap(bootstrap_path, f"datastore_mode: {datastore_mode}\n")
        assert load_bootstrap(str(bootstrap_path)).datastore_mode == datastore_mode

    _write_bootstrap(bootstrap_path, "datastore_mode: clustered\n")
    with pytest.raises(BootstrapConfigError, match="datastore_mode"):
        load_bootstrap(str(bootstrap_path))


def test_remote_mode_allows_nonloopback_database_url(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    remote_dsn = "postgresql://gobby:secret@100.64.0.10:5432/gobby"

    _write_bootstrap(
        bootstrap_path,
        f"datastore_mode: remote\ndatabase_url: {remote_dsn}\n",
    )
    assert load_bootstrap(str(bootstrap_path), resolve_database_url=True).database_url == remote_dsn

    _write_bootstrap(
        bootstrap_path,
        f"datastore_mode: local\ndatabase_url: {remote_dsn}\n",
    )
    with pytest.raises(BootstrapConfigError, match="local Docker-managed PostgreSQL"):
        load_bootstrap(str(bootstrap_path), resolve_database_url=True)


def test_remote_mode_rejects_loopback_database_url(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_path,
        "datastore_mode: remote\ndatabase_url: postgresql://gobby:secret@127.0.0.1:5432/gobby\n",
    )

    with pytest.raises(BootstrapConfigError, match="datastore_mode: local"):
        load_bootstrap(str(bootstrap_path), resolve_database_url=True)


@pytest.mark.parametrize(
    "database_url",
    [
        "mysql://gobby:secret@100.64.0.10:5432/gobby",
        "postgresql://100.64.0.10:5432/gobby",
        "postgresql://gobby@100.64.0.10:5432/gobby",
        "postgresql://gobby:secret@:5432/gobby",
    ],
)
def test_remote_mode_requires_full_postgresql_dsn(
    tmp_path: Path,
    database_url: str,
) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_path,
        f"datastore_mode: remote\ndatabase_url: {database_url}\n",
    )

    with pytest.raises(BootstrapConfigError, match="database_url"):
        load_bootstrap(str(bootstrap_path), resolve_database_url=True)


def test_datastore_mode_loads_from_bootstrap(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, "datastore_mode: remote\n")

    config = load_bootstrap(str(bootstrap_path))

    assert config.datastore_mode == "remote"


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
                "hub_backend": "legacy-value",
                "database_path": "/legacy/gobby.db",
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
    assert "hub_backend" not in bootstrap.to_config_dict()
    assert "database_path" not in bootstrap.to_config_dict()
