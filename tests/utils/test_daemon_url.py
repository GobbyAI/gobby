from __future__ import annotations

from pathlib import Path

import pytest

from gobby.utils.daemon_url import (
    DaemonUrlError,
    daemon_url,
    normalize_dial_host,
    resolve_daemon_url,
    validate_daemon_url,
)

pytestmark = pytest.mark.unit


def _write_bootstrap(path: Path, contents: str) -> Path:
    if (
        "files_home:" not in contents
        and "hub_daemon_url:" not in contents
        and "datastore_mode: remote" not in contents
    ):
        files_home = path.parent / "files"
        files_home.mkdir(exist_ok=True)
        contents = f"{contents}files_home: {files_home}\n"
    elif "datastore_mode: remote" in contents and "hub_daemon_url:" not in contents:
        contents = f"{contents}hub_daemon_url: http://hub.example.test:60887\n"
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_default_url_when_file_missing(tmp_path: Path) -> None:
    assert resolve_daemon_url(tmp_path / "missing.yaml", env={}) == "http://127.0.0.1:60887"


@pytest.mark.parametrize("host", ["", "0.0.0.0", "::", "::0", "[::]"])
def test_wildcard_hosts_normalize_to_loopback(tmp_path: Path, host: str) -> None:
    path = _write_bootstrap(
        tmp_path / "bootstrap.yaml", f"daemon_port: 60887\nbind_host: {host!r}\n"
    )

    assert resolve_daemon_url(path, env={}) == "http://127.0.0.1:60887"


def test_localhost_normalizes_to_numeric_loopback(tmp_path: Path) -> None:
    path = _write_bootstrap(
        tmp_path / "bootstrap.yaml", "daemon_port: 60887\nbind_host: localhost\n"
    )

    assert resolve_daemon_url(path, env={}) == "http://127.0.0.1:60887"


def test_custom_port_and_host_compose(tmp_path: Path) -> None:
    path = _write_bootstrap(
        tmp_path / "bootstrap.yaml", "daemon_port: 61234\nbind_host: 10.0.0.5\n"
    )

    assert resolve_daemon_url(path, env={}) == "http://10.0.0.5:61234"


@pytest.mark.parametrize("host", ["::1", "[::1]"])
def test_ipv6_literals_are_bracketed_once(tmp_path: Path, host: str) -> None:
    path = _write_bootstrap(
        tmp_path / "bootstrap.yaml", f"daemon_port: 61234\nbind_host: {host!r}\n"
    )

    assert resolve_daemon_url(path, env={}) == "http://[::1]:61234"


def test_env_url_beats_port_and_bootstrap(tmp_path: Path) -> None:
    path = _write_bootstrap(tmp_path / "bootstrap.yaml", "daemon_port: 61111\n")

    resolved = resolve_daemon_url(
        path,
        env={"GOBBY_DAEMON_URL": "http://override.invalid:1234/", "GOBBY_PORT": "61999"},
    )

    assert resolved == "http://override.invalid:1234"


def test_empty_env_url_falls_back_to_port(tmp_path: Path) -> None:
    path = _write_bootstrap(tmp_path / "bootstrap.yaml", "daemon_port: 61111\n")

    assert (
        resolve_daemon_url(path, env={"GOBBY_DAEMON_URL": " ", "GOBBY_PORT": "61999"})
        == "http://127.0.0.1:61999"
    )


@pytest.mark.parametrize("port", ["not-a-port", "", "70000"])
def test_invalid_env_port_falls_back_to_bootstrap(tmp_path: Path, port: str) -> None:
    path = _write_bootstrap(tmp_path / "bootstrap.yaml", "daemon_port: 61111\n")

    assert resolve_daemon_url(path, env={"GOBBY_PORT": port}) == "http://127.0.0.1:61111"


def test_deprecated_daemon_port_alias_is_honored(tmp_path: Path) -> None:
    path = _write_bootstrap(tmp_path / "bootstrap.yaml", "daemon_port: 61111\n")

    assert resolve_daemon_url(path, env={"GOBBY_DAEMON_PORT": "61998"}) == "http://127.0.0.1:61998"


def test_gobby_port_wins_over_deprecated_daemon_port_alias(tmp_path: Path) -> None:
    path = _write_bootstrap(tmp_path / "bootstrap.yaml", "daemon_port: 61111\n")

    resolved = resolve_daemon_url(
        path,
        env={"GOBBY_PORT": "61999", "GOBBY_DAEMON_PORT": "61998"},
    )

    assert resolved == "http://127.0.0.1:61999"


def test_bootstrap_daemon_url_beats_bind_host_endpoint(tmp_path: Path) -> None:
    path = _write_bootstrap(
        tmp_path / "bootstrap.yaml",
        "daemon_url: https://remote.invalid:7443/\ndaemon_port: 61111\nbind_host: 0.0.0.0\n",
    )

    assert resolve_daemon_url(path, env={}) == "https://remote.invalid:7443"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://remote.invalid:7443",
        "remote.invalid:7443",
        "http://remote.invalid:99999",
        "http://remote.invalid:7443?token=secret",
        "http://remote.invalid:7443#fragment",
    ],
)
def test_validate_daemon_url_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(DaemonUrlError):
        validate_daemon_url(url)


def test_invalid_env_url_raises(tmp_path: Path) -> None:
    path = _write_bootstrap(tmp_path / "bootstrap.yaml", "daemon_port: 61111\n")

    with pytest.raises(DaemonUrlError):
        resolve_daemon_url(path, env={"GOBBY_DAEMON_URL": "ftp://remote.invalid"})


def test_invalid_bootstrap_daemon_url_raises(tmp_path: Path) -> None:
    path = _write_bootstrap(tmp_path / "bootstrap.yaml", "daemon_url: ftp://remote.invalid\n")

    with pytest.raises(DaemonUrlError):
        resolve_daemon_url(path, env={})


def test_blank_bootstrap_daemon_url_raises(tmp_path: Path) -> None:
    path = _write_bootstrap(tmp_path / "bootstrap.yaml", "daemon_url: ' '\n")

    with pytest.raises(DaemonUrlError):
        resolve_daemon_url(path, env={})


def test_daemon_url_uses_current_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_bootstrap(tmp_path / "bootstrap.yaml", "daemon_port: 61111\n")
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setenv("GOBBY_PORT", "61999")
    monkeypatch.delenv("GOBBY_DAEMON_URL", raising=False)
    monkeypatch.delenv("GOBBY_DAEMON_PORT", raising=False)

    assert daemon_url() == "http://127.0.0.1:61999"


def test_normalize_dial_host_brackets_bare_ipv6() -> None:
    assert normalize_dial_host("2001:db8::1") == "[2001:db8::1]"
