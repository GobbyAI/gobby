"""The test gdaemon selector defaults to the installed binary and never trusts a stale build."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.fixtures.gdaemon_binary import (
    CHECKOUT_BINARY_ENV,
    CheckoutBinaryError,
    select_test_gdaemon,
)

pytestmark = pytest.mark.unit


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


@pytest.mark.parametrize("env", [{}, {CHECKOUT_BINARY_ENV: ""}, {CHECKOUT_BINARY_ENV: "installed"}])
def test_installed_binary_is_the_default(tmp_path: Path, env: dict[str, str]) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=1_000, binary_mtime_ns=2_000)

    assert select_test_gdaemon(repo, env, "gdaemon") is None


def test_checkout_opt_in_returns_a_fresh_debug_binary(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, source_mtime_ns=1_000, binary_mtime_ns=2_000)

    selected = select_test_gdaemon(repo, {CHECKOUT_BINARY_ENV: "checkout"}, "gdaemon")

    assert selected == repo / "target" / "debug" / "gdaemon"


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
