"""Bootstrap configuration tests."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gobby.cli.install_setup import ensure_daemon_config
from gobby.config import bootstrap as bootstrap_config
from gobby.config.bootstrap import (
    BootstrapConfig,
    BootstrapConfigError,
    FrontDoorConfig,
    backend_ports,
    load_bootstrap,
    resolve_bootstrap_path,
)
from gobby.config.bootstrap_io import (
    _merge_owner_fields,
    inject_local_files_home,
    update_bootstrap_yaml,
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


_PUBLIC_HOST = "100.64.0.10"


@pytest.mark.parametrize(
    ("bind_host", "front_door", "expected"),
    [
        ("localhost", "", "off"),
        ("127.0.0.1", "front_door:\n  enabled: true\n", "off"),
        ("::1", 'front_door:\n  tls:\n    mode: "off"\n', "off"),
        ("localhost", "front_door:\n  tls:\n    mode: off\n", "off"),
        ("localhost", "front_door:\n  tls:\n    mode: self-signed\n", "self-signed"),
        (_PUBLIC_HOST, "front_door:\n  tls:\n    mode: self-signed\n", "self-signed"),
        ("0.0.0.0", "front_door:\n  tls:\n    mode: files\n", "files"),
        (_PUBLIC_HOST, "", None),
        (_PUBLIC_HOST, "front_door:\n  enabled: true\n", None),
        (_PUBLIC_HOST, 'front_door:\n  tls:\n    mode: "off"\n', None),
        ("0.0.0.0", "front_door:\n  tls:\n    mode: off\n", None),
    ],
)
def test_front_door_tls_default_and_refusal(
    tmp_path: Path, bind_host: str, front_door: str, expected: str | None
) -> None:
    """Mirrors gcore `bootstrap::tests::front_door_tls_default_and_refusal`."""
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(bootstrap_path, f'bind_host: "{bind_host}"\n{front_door}')

    if expected is None:
        with pytest.raises(BootstrapConfigError, match=r"self-signed.*files"):
            load_bootstrap(str(bootstrap_path))
    else:
        assert load_bootstrap(str(bootstrap_path)).front_door.tls.mode == expected


def test_front_door_tls_block_parses_paths_and_sans(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_path,
        f"bind_host: {_PUBLIC_HOST}\n"
        "front_door:\n"
        "  tls:\n"
        "    mode: files\n"
        "    cert: /etc/gobby/hub.crt\n"
        "    key: /etc/gobby/hub.key\n"
        "    sans: [hub.example.test, 100.64.0.11, 'fd7a::1']\n",
    )

    tls = load_bootstrap(str(bootstrap_path)).front_door.tls

    assert tls == bootstrap_config.FrontDoorTlsConfig(
        mode="files",
        cert="/etc/gobby/hub.crt",
        key="/etc/gobby/hub.key",
        sans=("hub.example.test", "100.64.0.11", "fd7a::1"),
    )
    assert bootstrap_config.FrontDoorTlsConfig().mode == "off"


@pytest.mark.parametrize(
    ("tls", "message"),
    [
        ({"mode": "on"}, "front_door.tls.mode"),
        ({"mode": "files", "sans": ["0.0.0.0"]}, "unspecified address 0.0.0.0"),
        ({"mode": "self-signed", "sans": ["::"]}, "unspecified address ::"),
        ({"mode": "self-signed", "sans": "hub"}, "front_door.tls.sans must be a list"),
        ({"mode": "self-signed", "sans": [""]}, "front_door.tls.sans entries"),
        ({"mode": "files", "port": 1}, "front_door.tls has unknown keys: port"),
    ],
)
def test_front_door_tls_rejects_invalid_block(tmp_path: Path, tls: object, message: str) -> None:
    bootstrap_path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(
        bootstrap_path, yaml.safe_dump({"bind_host": _PUBLIC_HOST, "front_door": {"tls": tls}})
    )

    with pytest.raises(BootstrapConfigError, match=message):
        load_bootstrap(str(bootstrap_path))


@pytest.mark.parametrize("mode", [None, "off", "self-signed", "files"])
def test_disabled_front_door_is_loopback_only(tmp_path: Path, mode: str | None) -> None:
    """A disabled front door binds Python directly, so the TLS block would secure nothing."""
    tls = "" if mode is None else f'  tls:\n    mode: "{mode}"\n'
    public_path = tmp_path / "public" / "bootstrap.yaml"
    public_path.parent.mkdir()
    _write_bootstrap(
        public_path, f"bind_host: {_PUBLIC_HOST}\nfront_door:\n  enabled: false\n{tls}"
    )

    with pytest.raises(BootstrapConfigError, match="loopback bind_host") as refused:
        load_bootstrap(str(public_path))
    assert "front_door.enabled" in str(refused.value)

    for index, host in enumerate(("localhost", "127.0.0.1", "::1")):
        loopback_path = tmp_path / f"loopback-{index}" / "bootstrap.yaml"
        loopback_path.parent.mkdir()
        _write_bootstrap(
            loopback_path, f'bind_host: "{host}"\nfront_door:\n  enabled: false\n{tls}'
        )
        assert load_bootstrap(str(loopback_path)).front_door.enabled is False

    absent_path = tmp_path / "absent" / "bootstrap.yaml"
    absent_path.parent.mkdir()
    _write_bootstrap(absent_path, f"bind_host: {_PUBLIC_HOST}\n")
    with pytest.raises(BootstrapConfigError, match=r"self-signed.*files"):
        load_bootstrap(str(absent_path))


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


def test_run_mode_from_datastore_mode_and_hub(tmp_path: Path) -> None:
    """2.1.1: run_mode() derives the three modes; (remote, true) is rejected."""
    bootstrap_path = tmp_path / "bootstrap.yaml"

    _write_bootstrap(bootstrap_path, "datastore_mode: local\n")
    assert load_bootstrap(str(bootstrap_path)).run_mode() == "standalone"

    _write_bootstrap(bootstrap_path, "datastore_mode: local\nhub: true\n")
    assert load_bootstrap(str(bootstrap_path)).run_mode() == "hub"

    _write_bootstrap(bootstrap_path, "datastore_mode: remote\n")
    assert load_bootstrap(str(bootstrap_path)).run_mode() == "node"

    _write_bootstrap(bootstrap_path, "datastore_mode: remote\nhub: true\n")
    with pytest.raises(BootstrapConfigError, match="hub: true requires datastore_mode: local"):
        load_bootstrap(str(bootstrap_path))


def test_writers_emit_hub_flag(tmp_path: Path) -> None:
    """2.1.2: fresh and injected bootstraps carry hub: false; remote drops hub."""
    fresh = tmp_path / "fresh.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()
    with (
        patch("gobby.cli.install_setup.Path.expanduser", return_value=fresh),
        patch("gobby.cli.install_setup.get_install_dir", return_value=tmp_path / "missing-install"),
    ):
        ensure_daemon_config(files_home=files_home)
    assert yaml.safe_load(fresh.read_text(encoding="utf-8"))["hub"] is False
    assert load_bootstrap(str(fresh)).hub is False
    assert load_bootstrap(str(fresh)).run_mode() == "standalone"

    injected = tmp_path / "injected.yaml"
    injected.write_text("datastore_mode: local\ndaemon_port: 61111\n", encoding="utf-8")
    injected.chmod(0o600)
    inject_local_files_home(injected, files_home)
    assert yaml.safe_load(injected.read_text(encoding="utf-8"))["hub"] is False

    merged = _merge_owner_fields(
        {"datastore_mode": "local", "hub": True}, {"datastore_mode": "remote"}
    )
    assert "hub" not in merged


def test_api_key_fields_parse_and_write(tmp_path: Path) -> None:
    """4.2.9: api_key, api_key_id and hub_cert are absent by default and round-trip."""
    path = tmp_path / "bootstrap.yaml"
    _write_bootstrap(path, "datastore_mode: local\n")
    absent = load_bootstrap(str(path))
    assert (absent.api_key, absent.api_key_id, absent.hub_cert) == (None, None, None)

    def _enroll(data: dict[str, object]) -> None:
        data["api_key"] = "gobby_example"
        data["api_key_id"] = "0b6c8f5e-2d7a-4c1e-9f3b-5a8d7e6c4b21"
        data["hub_cert"] = str(tmp_path / "hub.pem")

    update_bootstrap_yaml(path, _enroll)

    loaded = load_bootstrap(str(path))
    assert loaded.api_key == "gobby_example"
    assert loaded.api_key_id == "0b6c8f5e-2d7a-4c1e-9f3b-5a8d7e6c4b21"
    assert loaded.hub_cert == str(tmp_path / "hub.pem")

    _write_bootstrap(path, "datastore_mode: local\napi_key: 42\n")
    with pytest.raises(BootstrapConfigError, match="api_key"):
        load_bootstrap(str(path))


def test_resolve_bootstrap_path_prefers_sibling_bootstrap(tmp_path: Path) -> None:
    legacy = tmp_path / "config.yaml"
    sibling = tmp_path / "bootstrap.yaml"
    with patch.object(bootstrap_config, "default_bootstrap_path", return_value=sibling):
        assert resolve_bootstrap_path(None) == sibling
    assert resolve_bootstrap_path(str(legacy)) == legacy
    assert resolve_bootstrap_path(str(sibling)) == sibling

    _write_bootstrap(sibling, "datastore_mode: local\n")
    assert resolve_bootstrap_path(str(legacy)) == sibling
    assert resolve_bootstrap_path("~/bootstrap.yaml") == Path.home() / "bootstrap.yaml"
