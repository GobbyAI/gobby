"""Bootstrap configuration tests."""

from pathlib import Path

import pytest
import yaml

from gobby.config.bootstrap import (
    BootstrapConfig,
    BootstrapConfigError,
    FrontDoorConfig,
    backend_ports,
    load_bootstrap,
)


def _write_bootstrap(path: Path, content: str) -> None:
    if (
        "files_home:" not in content
        and "hub_daemon_url:" not in content
        and "datastore_mode: clustered" not in content
        and "datastore_mode: remote" not in content
    ):
        files_home = path.parent / "files"
        files_home.mkdir(exist_ok=True)
        content = f"{content}files_home: {files_home}\n"
    elif "datastore_mode: remote" in content and "hub_daemon_url:" not in content:
        content = f"{content}hub_daemon_url: http://hub.example.test:60887\n"
    path.write_text(content)
    path.chmod(0o600)


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


def test_ui_exposure_mode_loads_from_bootstrap(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, "ui_expose: tailscale\n")

    assert load_bootstrap(str(bootstrap_path)).ui_expose == "tailscale"


def test_ui_exposure_mode_rejects_unknown_value(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, "ui_expose: funnel\n")

    with pytest.raises(BootstrapConfigError, match="ui_expose"):
        load_bootstrap(str(bootstrap_path))


def test_ui_exposure_is_machine_local_only() -> None:
    assert "ui_expose" not in BootstrapConfig(ui_expose="tailscale").to_config_dict()


def test_loading_a_missing_bootstrap_never_writes_one(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    assert load_bootstrap(str(home / "bootstrap.yaml")) == BootstrapConfig()
    with pytest.raises(BootstrapConfigError, match="database_url is required"):
        load_bootstrap(str(home / "bootstrap.yaml"), resolve_database_url=True)

    assert list(home.iterdir()) == []


def test_front_door_defaults_enabled_with_no_routes(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, "daemon_port: 60887\n")

    config = load_bootstrap(str(bootstrap_path))

    assert config.front_door == FrontDoorConfig(enabled=True, routes={})
    assert BootstrapConfig().front_door.enabled is True
    assert "front_door" not in config.to_config_dict()


def test_front_door_block_parses_routes_and_accepts_unknown_families(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_path,
        "front_door:\n"
        "  enabled: false\n"
        "  routes:\n"
        "    health: native\n"
        "    terminal_ws: proxy\n"
        "    future_family: compare\n",
    )

    front_door = load_bootstrap(str(bootstrap_path)).front_door

    assert front_door.enabled is False
    assert dict(front_door.routes) == {
        "health": "native",
        "terminal_ws": "proxy",
        "future_family": "compare",
    }


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"routes": {"health": "sideways"}}, "front_door.routes.health"),
        ({"enabled": "maybe"}, "front_door.enabled"),
        ({"enable": False}, "unknown keys: enable"),
        (True, "front_door must be a mapping"),
        ({"routes": ["health"]}, "front_door.routes must be a mapping"),
        ({"routes": ""}, "front_door.routes must be a mapping"),
    ],
)
def test_front_door_rejects_invalid_block(tmp_path: Path, block: object, message: str) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, yaml.safe_dump({"front_door": block}))

    with pytest.raises(BootstrapConfigError, match=message):
        load_bootstrap(str(bootstrap_path))


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("true", True),
        ("yes", True),
        ('"yes"', True),
        ("On", True),
        ("false", False),
        ("no", False),
        ("'off'", False),
        ("FALSE", False),
        ("maybe", None),
        ("1", None),
    ],
)
def test_front_door_enabled_matches_gcore(
    tmp_path: Path, literal: str, expected: bool | None
) -> None:
    """Mirrors gcore `bootstrap::tests::front_door_enabled_matches_python`."""
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, f"front_door:\n  enabled: {literal}\n")

    if expected is None:
        with pytest.raises(BootstrapConfigError, match="front_door.enabled"):
            load_bootstrap(str(bootstrap_path))
    else:
        assert load_bootstrap(str(bootstrap_path)).front_door.enabled is expected


def test_backend_ports_offset() -> None:
    assert backend_ports(60887, 60888) == (60987, 60988)
    assert backend_ports(61000, 61001) == (61100, 61101)
    with pytest.raises(BootstrapConfigError, match="exceed 65535"):
        backend_ports(65500, 60888)
