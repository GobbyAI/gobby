"""The test gdaemon selector defaults to the installed binary and never trusts a stale build."""

from __future__ import annotations

import inspect
import os
import subprocess
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from tests.fixtures.gdaemon_binary import (
    CHECKOUT_BINARY_ENV,
    CheckoutBinaryError,
    checkout_debug_dir,
    select_test_gdaemon,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("selector", [None, "checkout"])
def test_pair_node_launches_prepared_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selector: str | None
) -> None:
    """The enrolled node uses the same prepared binary selection as the hub."""
    import yaml

    from gobby.utils.native_bin import NATIVE_BIN_DIR_ENV, native_bin_name
    from tests.e2e import test_hub_node_pair as pair
    from tests.e2e.conftest import DaemonInstance

    if selector is None:
        monkeypatch.delenv(CHECKOUT_BINARY_ENV, raising=False)
    else:
        monkeypatch.setenv(CHECKOUT_BINARY_ENV, selector)
    binary_dir = tmp_path / "selected-binaries"
    env = {NATIVE_BIN_DIR_ENV: str(binary_dir), "DATABASE_URL": "parent-db"}
    hub = MagicMock(spec=DaemonInstance)
    hub.http_port = 12345
    hub.cert_path = tmp_path / "hub.crt"
    hub.cert_path.write_text("isolated certificate", encoding="utf-8")
    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = None

    def enrolled_login(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
        bootstrap_path = home / "bootstrap.yaml"
        bootstrap = yaml.safe_load(bootstrap_path.read_text(encoding="utf-8"))
        bootstrap.update(api_key="node-key", api_key_id="node-key-id")
        bootstrap_path.write_text(yaml.safe_dump(bootstrap), encoding="utf-8")
        (home / "machine_id").write_text("node-machine", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with (
        patch.object(pair, "_login", side_effect=enrolled_login),
        patch.object(pair, "pem_fingerprint", return_value="fingerprint"),
        patch.object(pair, "prepare_daemon_env", return_value=env) as prepare,
        patch.object(subprocess, "Popen", return_value=process) as popen,
    ):
        node_fixture = cast(
            Callable[[DaemonInstance, Path], Generator[pair.EnrolledNode]],
            inspect.unwrap(pair.pair_node),
        )
        fixture = node_fixture(hub, tmp_path)
        try:
            node = next(fixture)
            prepare.assert_called_once_with(home_dir=node.home)
            assert popen.call_args.args[0] == [
                str(binary_dir / native_bin_name("gdaemon")),
                "serve",
            ]
            assert popen.call_args.kwargs["env"]["GOBBY_HOME"] == str(node.home)
            assert "DATABASE_URL" not in popen.call_args.kwargs["env"]
        finally:
            fixture.close()

    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5.0)


@pytest.mark.parametrize("configured", [None, "cargo-cache", "absolute"])
@pytest.mark.parametrize("binary_name", ["gcode", "gdaemon", "ghook"])
def test_runtime_boundary_resolves_cargo_debug_binaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured: str | None, binary_name: str
) -> None:
    from tests.e2e import test_runtime_boundary

    repo_root = tmp_path / "checkout"
    if configured is None:
        monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
        target = repo_root / "target"
    else:
        target = tmp_path / "cargo-cache" if configured == "absolute" else repo_root / configured
        monkeypatch.setenv(
            "CARGO_TARGET_DIR", str(target) if configured == "absolute" else configured
        )
    debug = target / "debug"
    debug.mkdir(parents=True)
    binary = debug / binary_name
    binary.write_bytes(b"fixture binary")
    monkeypatch.setattr(test_runtime_boundary, "_repo_root", lambda: repo_root)

    assert checkout_debug_dir(repo_root, os.environ) == debug
    assert test_runtime_boundary._native_bin(binary_name) == binary


def test_single_active_daemon_keeps_parent_cargo_target_after_environment_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.e2e import test_single_active_daemon

    target = tmp_path / "cargo-cache"
    monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
    home = tmp_path / "daemon-home"
    (home / "logs").mkdir(parents=True)

    def cleaned_env(*, home_dir: Path) -> dict[str, str]:
        assert home_dir == home
        return {"PATH": "base-path"}

    monkeypatch.setattr(test_single_active_daemon, "prepare_daemon_env", cleaned_env)
    with patch("tests.e2e.test_single_active_daemon.subprocess.Popen") as spawn:
        spawn.return_value.pid = 123
        daemon = test_single_active_daemon._spawn_daemon(
            tmp_path, home / "bootstrap.yaml", 12345, 12346
        )

    assert daemon.env["PATH"] == f"{target / 'debug'}:base-path"
    assert "CARGO_TARGET_DIR" not in daemon.env


def _write(path: Path, mtime_ns: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _checkout(tmp_path: Path, *, source_mtime_ns: int, binary_mtime_ns: int | None) -> Path:
    _write(tmp_path / "crates" / "gcore" / "src" / "schema" / "runner.rs", source_mtime_ns)
    _write(tmp_path / "crates" / "gdaemon" / "src" / "main.rs", source_mtime_ns - 1)
    if binary_mtime_ns is not None:
        _write(tmp_path / "target" / "debug" / "gdaemon", binary_mtime_ns)
    return tmp_path


@pytest.mark.parametrize(
    "relative",
    [
        "crates/gcore/src/grant/tests.rs",
        "crates/gcore/src/grant/cache_tests.rs",
        "crates/gcore/tests/credential.rs",
        "crates/gdaemon/benches/startup.rs",
        "crates/gdaemon/examples/inspect.rs",
    ],
)
def test_newer_test_only_file_does_not_stale_checkout_binary(tmp_path: Path, relative: str) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=1_000, binary_mtime_ns=2_000)
    _write(repo / relative, 3_000)
    assert select_test_gdaemon(repo, {CHECKOUT_BINARY_ENV: "checkout"}, "gdaemon") == (
        repo / "target" / "debug" / "gdaemon"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "crates/gcore/src/grant/cache.rs",
        "crates/gcore/assets/schema/migrations/999_example.sql",
        "crates/gcore/build.rs",
        "crates/gdaemon/Cargo.toml",
        "Cargo.toml",
        "Cargo.lock",
    ],
)
def test_newer_binary_input_rejects_checkout_binary(tmp_path: Path, relative: str) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=1_000, binary_mtime_ns=2_000)
    _write(repo / relative, 3_000)
    with pytest.raises(CheckoutBinaryError, match="older than") as caught:
        select_test_gdaemon(repo, {CHECKOUT_BINARY_ENV: "checkout"}, "gdaemon")
    assert relative in str(caught.value)


def test_inline_test_module_retains_production_file_freshness_check(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=1_000, binary_mtime_ns=2_000)
    source = _write(repo / "crates/gcore/src/grant/cache.rs", 3_000)
    source.write_text("pub fn production() {}\n#[cfg(test)]\nmod tests {}\n")
    os.utime(source, ns=(3_000, 3_000))
    with pytest.raises(CheckoutBinaryError, match="cache.rs"):
        select_test_gdaemon(repo, {CHECKOUT_BINARY_ENV: "checkout"}, "gdaemon")


@pytest.mark.parametrize("env", [{}, {CHECKOUT_BINARY_ENV: ""}, {CHECKOUT_BINARY_ENV: "installed"}])
def test_installed_binary_is_the_default(tmp_path: Path, env: dict[str, str]) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=1_000, binary_mtime_ns=2_000)

    assert select_test_gdaemon(repo, env, "gdaemon") is None


def test_checkout_opt_in_returns_a_fresh_debug_binary(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=1_000, binary_mtime_ns=2_000)

    selected = select_test_gdaemon(repo, {CHECKOUT_BINARY_ENV: "checkout"}, "gdaemon")

    assert selected == repo / "target" / "debug" / "gdaemon"


@pytest.mark.parametrize("relative", [False, True])
def test_checkout_uses_cargo_target_directory(tmp_path: Path, relative: bool) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=1_000, binary_mtime_ns=None)
    target = repo / "cargo-output" if relative else tmp_path / "cargo-output"
    binary = _write(target / "debug" / "gdaemon", 2_000)
    env = {
        CHECKOUT_BINARY_ENV: "checkout",
        "CARGO_TARGET_DIR": "cargo-output" if relative else str(target),
    }

    assert select_test_gdaemon(repo, env, "gdaemon") == binary


def test_checkout_opt_in_rejects_a_missing_binary(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=1_000, binary_mtime_ns=None)

    with pytest.raises(CheckoutBinaryError, match="does not exist"):
        select_test_gdaemon(repo, {CHECKOUT_BINARY_ENV: "checkout"}, "gdaemon")


def test_checkout_opt_in_rejects_a_binary_older_than_its_sources(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=3_000, binary_mtime_ns=2_000)

    with pytest.raises(CheckoutBinaryError, match="runner.rs"):
        select_test_gdaemon(repo, {CHECKOUT_BINARY_ENV: "checkout"}, "gdaemon")


def test_unknown_selector_value_is_an_error(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=1_000, binary_mtime_ns=2_000)

    with pytest.raises(ValueError, match="not recognized"):
        select_test_gdaemon(repo, {CHECKOUT_BINARY_ENV: "checkou"}, "gdaemon")
