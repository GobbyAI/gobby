"""Checkout build-output paths used by the e2e runtime-boundary and daemon tests."""

from __future__ import annotations

import inspect
import os
import subprocess
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from tests.fixtures.gdaemon_binary import checkout_debug_dir

pytestmark = pytest.mark.unit


def test_pair_node_launches_prepared_binary(tmp_path: Path) -> None:
    """The enrolled node uses the same prepared binary selection as the hub."""
    import yaml

    from gobby.utils.native_bin import NATIVE_BIN_DIR_ENV, native_bin_name
    from tests.e2e import test_hub_node_pair as pair
    from tests.e2e.conftest import DaemonInstance

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
    from gobby.utils.native_bin import NATIVE_BIN_DIR_ENV, native_bin_name
    from tests.e2e import test_single_active_daemon

    target = tmp_path / "cargo-cache"
    monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
    home = tmp_path / "daemon-home"
    (home / "logs").mkdir(parents=True)
    # The real prepare_daemon_env always pins the isolated native bin directory.
    native_bin = tmp_path / "native-bin"
    native_bin.mkdir()
    (native_bin / native_bin_name("gdaemon")).write_bytes(b"fixture binary")

    def cleaned_env(*, home_dir: Path) -> dict[str, str]:
        assert home_dir == home
        return {"PATH": "base-path", NATIVE_BIN_DIR_ENV: str(native_bin)}

    monkeypatch.setattr(test_single_active_daemon, "prepare_daemon_env", cleaned_env)
    with patch("tests.e2e.test_single_active_daemon.subprocess.Popen") as spawn:
        spawn.return_value.pid = 123
        daemon = test_single_active_daemon._spawn_daemon(
            tmp_path, home / "bootstrap.yaml", 12345, 12346
        )

    assert daemon.env["PATH"] == f"{target / 'debug'}:base-path"
    assert "CARGO_TARGET_DIR" not in daemon.env
