"""Hub files_home / hub_daemon_url bootstrap and path contract."""

from __future__ import annotations

import os
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig, BootstrapConfigError, load_bootstrap
from gobby.config.bootstrap_io import (
    inject_local_files_home,
    read_bootstrap_yaml,
    write_bootstrap_yaml,
)
from gobby.paths import (
    FilesHomeError,
    FilesHomeNotOnThisDaemonError,
    get_files_home,
    publish_files_home_descendant,
    require_files_home,
)

pytestmark = pytest.mark.unit


def _write_raw(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _local_mapping(files_home: Path, **extra: object) -> dict[str, object]:
    mapping: dict[str, object] = {
        "datastore_mode": "local",
        "files_home": str(files_home),
    }
    mapping.update(extra)
    return mapping


def _remote_mapping(
    url: str = "http://hub.example.test:60887", **extra: object
) -> dict[str, object]:
    mapping: dict[str, object] = {
        "datastore_mode": "remote",
        "hub_daemon_url": url,
    }
    mapping.update(extra)
    return mapping


def _mp_update_field(path: str, key: str, value: object) -> None:
    from gobby.config.bootstrap_io import update_bootstrap_yaml as update

    def updater(data: dict[str, object]) -> None:
        data[key] = value

    update(Path(path), updater)


def _mp_write_mapping(path: str, mapping: dict[str, object]) -> None:
    from gobby.config.bootstrap_io import write_bootstrap_yaml as write

    write(Path(path), mapping)


def test_local_bootstrap_requires_absolute_files_home_and_refuses_hub_url(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bootstrap.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()

    _write_raw(path, "datastore_mode: local\n")
    with pytest.raises(BootstrapConfigError, match="files_home"):
        load_bootstrap(str(path))

    _write_raw(path, f"datastore_mode: local\nfiles_home: {files_home}\n")
    loaded = load_bootstrap(str(path))
    assert loaded.files_home == str(files_home)
    assert loaded.hub_daemon_url is None

    _write_raw(
        path,
        f"datastore_mode: local\nfiles_home: {files_home}\n"
        "hub_daemon_url: http://hub.example.test:60887\n",
    )
    with pytest.raises(BootstrapConfigError, match="hub_daemon_url"):
        load_bootstrap(str(path))


@pytest.mark.parametrize("value", ["files", "./files", "~/files", "~"])
def test_local_files_home_rejects_relative_and_tilde(tmp_path: Path, value: str) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_raw(path, f"datastore_mode: local\nfiles_home: {value}\n")
    with pytest.raises(BootstrapConfigError, match="files_home"):
        load_bootstrap(str(path))


def test_local_files_home_rejects_filesystem_root(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_raw(path, "datastore_mode: local\nfiles_home: /\n")
    with pytest.raises(BootstrapConfigError, match="files_home"):
        load_bootstrap(str(path))


def test_remote_bootstrap_requires_hub_url_and_refuses_files_home(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()

    _write_raw(path, "datastore_mode: remote\n")
    with pytest.raises(BootstrapConfigError, match="hub_daemon_url"):
        load_bootstrap(str(path))

    _write_raw(path, "datastore_mode: remote\nhub_daemon_url: http://hub.example.test:60887\n")
    loaded = load_bootstrap(str(path))
    assert loaded.hub_daemon_url == "http://hub.example.test:60887"
    assert loaded.files_home is None

    _write_raw(
        path,
        "datastore_mode: remote\n"
        "hub_daemon_url: http://hub.example.test:60887\n"
        f"files_home: {files_home}\n",
    )
    with pytest.raises(BootstrapConfigError, match="files_home"):
        load_bootstrap(str(path))


@pytest.mark.parametrize(
    "url",
    [
        "http://hub.example.test",
        "https://hub.example.test:7443",
        "http://hub.example.test:60887/",
        "http://192.0.2.10:60887",
    ],
)
def test_hub_daemon_url_accepts_http_origins(tmp_path: Path, url: str) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_raw(path, f"datastore_mode: remote\nhub_daemon_url: {url}\n")
    loaded = load_bootstrap(str(path))
    assert loaded.hub_daemon_url is not None
    assert loaded.hub_daemon_url.startswith(("http://", "https://"))
    assert not loaded.hub_daemon_url.endswith("/")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://hub.example.test",
        "http://user:pass@hub.example.test",
        "http://hub.example.test/path",
        "http://hub.example.test?q=1",
        "http://hub.example.test#frag",
        "not-a-url",
        "http://",
    ],
)
def test_hub_daemon_url_rejects_non_origins(tmp_path: Path, url: str) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_raw(path, f"datastore_mode: remote\nhub_daemon_url: {url}\n")
    with pytest.raises(BootstrapConfigError, match="hub_daemon_url"):
        load_bootstrap(str(path))


def test_hub_daemon_url_refuses_this_process_origin(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.yaml"
    _write_raw(
        path,
        "datastore_mode: remote\n"
        "bind_host: localhost\n"
        "daemon_port: 60887\n"
        "hub_daemon_url: http://localhost:60887\n",
    )
    with pytest.raises(BootstrapConfigError, match="own"):
        load_bootstrap(str(path))

    _write_raw(
        path,
        "datastore_mode: remote\n"
        "daemon_url: http://hub.example.test:7443\n"
        "hub_daemon_url: http://hub.example.test:7443\n",
    )
    with pytest.raises(BootstrapConfigError, match="own"):
        load_bootstrap(str(path))


def test_missing_bootstrap_is_not_a_files_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    loaded = load_bootstrap(str(tmp_path / "missing.yaml"))
    assert loaded.files_home is None
    assert loaded.hub_daemon_url is None
    assert get_files_home() is None
    with pytest.raises(FilesHomeError):
        require_files_home()


def test_require_files_home_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "gobby"
    home.mkdir()
    files_home = tmp_path / "files"
    files_home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))

    write_bootstrap_yaml(home / "bootstrap.yaml", _local_mapping(files_home))
    assert require_files_home() == files_home.resolve()
    assert get_files_home() == files_home.resolve()

    missing = tmp_path / "absent"
    write_bootstrap_yaml(home / "bootstrap.yaml", _local_mapping(missing))
    with pytest.raises(FilesHomeError):
        require_files_home()

    write_bootstrap_yaml(home / "bootstrap.yaml", _remote_mapping())
    with pytest.raises(FilesHomeNotOnThisDaemonError):
        require_files_home()
    assert get_files_home() is None


def test_require_files_home_detects_root_swap_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gobby"
    home.mkdir()
    files_home = tmp_path / "files"
    files_home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    write_bootstrap_yaml(home / "bootstrap.yaml", _local_mapping(files_home))

    require_files_home()
    files_home.rename(tmp_path / "files.old")
    files_home.mkdir()

    with pytest.raises(FilesHomeError):
        publish_files_home_descendant("USER.md", b"hi")
    assert not (files_home / "USER.md").exists()


def test_require_files_home_detects_ancestor_swap_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gobby"
    home.mkdir()
    bind = tmp_path / "bind"
    files_home = bind / "files"
    files_home.mkdir(parents=True)
    monkeypatch.setenv("GOBBY_HOME", str(home))
    write_bootstrap_yaml(home / "bootstrap.yaml", _local_mapping(files_home))

    require_files_home()
    bind.rename(tmp_path / "bind.old")
    files_home.mkdir(parents=True)

    with pytest.raises(FilesHomeError):
        publish_files_home_descendant("notes/x.md", b"hi")
    assert not (files_home / "notes" / "x.md").exists()


def test_local_and_remote_writers_persist_mode_fields(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()

    write_bootstrap_yaml(path, _local_mapping(files_home, daemon_port=61111))
    published = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert published["files_home"] == str(files_home)
    assert load_bootstrap(str(path)).files_home == str(files_home)

    write_bootstrap_yaml(path, _remote_mapping("https://hub.example.test:7443"))
    published = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert published["hub_daemon_url"] == "https://hub.example.test:7443"
    assert load_bootstrap(str(path)).hub_daemon_url == "https://hub.example.test:7443"


def test_write_postgres_defaults_keeps_files_home(tmp_path: Path) -> None:
    from gobby.config.postgres_bootstrap import write_postgres_defaults

    files_home = tmp_path / "files"
    files_home.mkdir()
    write_bootstrap_yaml(tmp_path / "bootstrap.yaml", _local_mapping(files_home))
    write_postgres_defaults(
        gobby_home=tmp_path,
        database_url="postgresql://gobby:secret@localhost:60891/gobby",
    )
    loaded = load_bootstrap(str(tmp_path / "bootstrap.yaml"))
    assert loaded.files_home == str(files_home)
    assert loaded.database_url == "postgresql://gobby:secret@localhost:60891/gobby"


def test_ensure_daemon_config_persists_caller_files_home(tmp_path: Path) -> None:
    from gobby.cli.install_setup import ensure_daemon_config

    target = tmp_path / "bootstrap.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()
    shared = tmp_path / "install" / "shared" / "config" / "bootstrap.yaml"
    shared.parent.mkdir(parents=True)
    shared.write_text("datastore_mode: local\ndaemon_port: 60887\nbind_host: localhost\n")

    with (
        patch("gobby.cli.install_setup.Path.expanduser", return_value=target),
        patch("gobby.cli.install_setup.get_install_dir", return_value=tmp_path / "install"),
        patch("shutil.copy2") as copy2,
    ):
        result = ensure_daemon_config(files_home=files_home)

    assert result["created"] is True
    copy2.assert_not_called()
    assert (tmp_path / ".bootstrap.yaml.lock").exists()
    loaded = load_bootstrap(str(target))
    assert loaded.files_home == str(files_home.resolve())
    assert loaded.datastore_mode == "local"


def test_ensure_daemon_config_generated_fallback_includes_files_home(tmp_path: Path) -> None:
    from gobby.cli.install_setup import ensure_daemon_config

    target = tmp_path / "bootstrap.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()

    with (
        patch("gobby.cli.install_setup.Path.expanduser", return_value=target),
        patch("gobby.cli.install_setup.get_install_dir", return_value=tmp_path / "missing-install"),
    ):
        result = ensure_daemon_config(files_home=files_home)

    assert result["created"] is True
    assert result["source"] == "generated"
    assert load_bootstrap(str(target)).files_home == str(files_home.resolve())


def test_to_config_dict_and_daemon_config_carry_mode_fields(tmp_path: Path) -> None:
    files_home = tmp_path / "files"
    local = BootstrapConfig(files_home=str(files_home))
    local_dict = local.to_config_dict()
    assert local_dict["files_home"] == str(files_home)
    assert "hub_daemon_url" in local_dict
    carried = DaemonConfig.model_validate(local_dict)
    assert carried.files_home == str(files_home)
    assert carried.hub_daemon_url is None

    remote = BootstrapConfig(datastore_mode="remote", hub_daemon_url="http://hub.example.test:9")
    remote_dict = remote.to_config_dict()
    assert remote_dict["hub_daemon_url"] == "http://hub.example.test:9"
    carried_remote = DaemonConfig.model_validate(remote_dict)
    assert carried_remote.hub_daemon_url == "http://hub.example.test:9"
    assert carried_remote.files_home is None


def test_concurrent_update_keeps_owner_fields(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()
    write_bootstrap_yaml(path, _local_mapping(files_home, daemon_port=60887))

    ctx = get_context("spawn")
    first = ctx.Process(target=_mp_update_field, args=(str(path), "ui_port", 61112))
    second = ctx.Process(target=_mp_update_field, args=(str(path), "websocket_port", 61113))
    first.start()
    second.start()
    first.join(timeout=15)
    second.join(timeout=15)
    assert first.exitcode == 0
    assert second.exitcode == 0

    published = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert published["files_home"] == str(files_home)
    assert published["ui_port"] == 61112
    assert published["websocket_port"] == 61113
    assert load_bootstrap(str(path)).files_home == str(files_home)


def test_update_versus_create_and_rollback_keep_owner_fields(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()
    other_home = tmp_path / "files-b"
    other_home.mkdir()
    write_bootstrap_yaml(path, _local_mapping(files_home, daemon_port=60887))

    ctx = get_context("spawn")
    writer = ctx.Process(
        target=_mp_write_mapping,
        args=(str(path), _local_mapping(other_home, ui_port=61200)),
    )
    updater = ctx.Process(target=_mp_update_field, args=(str(path), "websocket_port", 61201))
    writer.start()
    updater.start()
    writer.join(timeout=15)
    updater.join(timeout=15)
    assert writer.exitcode == 0
    assert updater.exitcode == 0

    published = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert published["files_home"] in {str(files_home), str(other_home)}
    assert load_bootstrap(str(path)).files_home == published["files_home"]

    previous = _local_mapping(files_home, services_bind_address="127.0.0.1")
    rollback = ctx.Process(target=_mp_write_mapping, args=(str(path), previous))
    concurrent = ctx.Process(target=_mp_update_field, args=(str(path), "ui_port", 61300))
    rollback.start()
    concurrent.start()
    rollback.join(timeout=15)
    concurrent.join(timeout=15)
    assert rollback.exitcode == 0
    assert concurrent.exitcode == 0
    final = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert final["files_home"]
    assert load_bootstrap(str(path)).files_home == final["files_home"]


def test_inject_legacy_local_mapping_without_load_bootstrap(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()
    _write_raw(path, "datastore_mode: local\ndaemon_port: 61111\n")

    with patch("gobby.config.bootstrap.load_bootstrap") as loader:
        inject_local_files_home(path, files_home)
        loader.assert_not_called()

    loaded = load_bootstrap(str(path))
    assert loaded.files_home == str(files_home.resolve())
    assert loaded.daemon_port == 61111


def test_present_invalid_bootstrap_is_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.yaml"

    _write_raw(path, "not: [valid: yaml\n")
    with pytest.raises(BootstrapConfigError):
        load_bootstrap(str(path))

    _write_raw(path, "- just\n- a\n- list\n")
    with pytest.raises(BootstrapConfigError, match="mapping"):
        load_bootstrap(str(path))

    path.write_bytes(b"\xff\xfe\x00files_home")
    path.chmod(0o600)
    with pytest.raises(BootstrapConfigError):
        load_bootstrap(str(path))

    path.unlink()
    _write_raw(path, "datastore_mode: local\nfiles_home: /var/lib/gobby/files\n")
    os.chmod(path, 0o000)
    try:
        with pytest.raises(BootstrapConfigError):
            load_bootstrap(str(path))
    finally:
        os.chmod(path, 0o600)


def test_installed_template_documents_files_home() -> None:
    template = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "gobby"
        / "install"
        / "shared"
        / "config"
        / "bootstrap.yaml"
    )
    text = template.read_text(encoding="utf-8")
    assert "files_home" in text
    assert "datastore_mode" in text


def test_read_bootstrap_yaml_on_valid_local_mapping(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.yaml"
    files_home = tmp_path / "files"
    files_home.mkdir()
    write_bootstrap_yaml(path, _local_mapping(files_home))
    data = read_bootstrap_yaml(path)
    assert data["files_home"] == str(files_home)
