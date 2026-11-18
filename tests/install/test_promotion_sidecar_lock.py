"""The post-swap hook of the binary promotion entry points runs under the store lock.

The hook writes sidecars and identity stamps once the new bytes are in place. Each test drives
the public promotion entry points against real files in pytest temp directories, and probes the
store lock through the same per-tool lock file the promotion guard takes: a probe that acquires
that lock means the guard was not held at that moment.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from gobby.install import bin_freshness_promotion as promotion
from gobby.install.bin_freshness_locks import try_acquire_native_bin_lock
from gobby.install.bin_generations import list_generations
from gobby.install.bin_release_identity import BinaryReleaseIdentity
from gobby.install.bin_set_coherence import BinarySetCoherenceError, promote_workspace_binary_set

pytestmark = pytest.mark.unit

STORE_LOCK = "generations"
SET_MEMBERS = ("gcode", "gdaemon", "ghook")
CRATE_DIRS = ("gcode", "gdaemon", "ghook", "gclient", "gmcp", "gterminal", "gcore")
IDENTITY = {
    "runner_protocol": 1,
    "baseline_version": 420,
    "baseline_checksum": "baseline",
    "latest_version": 468,
    "latest_checksum": "latest",
    "assets_root_hash": "root",
}


def _store_lock_is_free(bin_dir: Path) -> bool:
    """True when no one holds the generation store lock; the probe releases what it takes."""
    lock = try_acquire_native_bin_lock(STORE_LOCK, bin_dir=bin_dir)
    if lock is None:
        return False
    lock.release()
    return True


def _write_bytes(path: Path, payload: bytes, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def _identity(version: str) -> BinaryReleaseIdentity:
    return BinaryReleaseIdentity(version, f"artifact:{version}", f"sha-{version}")


def _member_stub(path: Path, version: str) -> Path:
    """A shell stub that reports a version and the shared identity, as a workspace binary does."""
    payload = json.dumps(IDENTITY, separators=(",", ":"), sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then\n'
        f"printf '%s\\n' '{path.name} {version}'\nelse\n"
        f"printf '%s\\n' '{payload}'\nfi\n"
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    for name in CRATE_DIRS:
        crate = root / "crates" / name
        (crate / "src").mkdir(parents=True)
        dependency = '\n[dependencies]\ngobby-core = { path = "../gcore" }\n'
        if name == "gclient":
            dependency += 'gobby-terminal = { path = "../gterminal" }\n'
        (crate / "Cargo.toml").write_text(
            f'[package]\nname = "{name}"\nversion = "1.0.0"\n'
            + (dependency if name != "gcore" else "")
        )
        (crate / "src" / "lib.rs").write_text("pub fn initial() {}\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


def test_workspace_set_after_swap_runs_once_with_new_bytes_under_the_store_lock(
    workspace: Path, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    candidates = {name: _member_stub(tmp_path / "1.0.0" / name, "1.0.0") for name in SET_MEMBERS}
    calls: list[tuple[dict[str, bool], bool]] = []

    def after_swap() -> None:
        in_place = {
            name: (bin_dir / name).read_bytes() == candidates[name].read_bytes()
            for name in SET_MEMBERS
        }
        calls.append((in_place, _store_lock_is_free(bin_dir)))

    promote_workspace_binary_set(
        candidates, bin_dir=bin_dir, workspace_root=workspace, after_swap=after_swap
    )

    assert calls == [(dict.fromkeys(SET_MEMBERS, True), False)]


def test_single_binary_after_swap_runs_once_with_new_bytes_under_the_store_lock(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    destination = _write_bytes(bin_dir / "gdaemon", b"gdaemon-old")
    source = _write_bytes(tmp_path / "source" / "gdaemon", b"gdaemon-new")
    calls: list[tuple[bool, bool]] = []

    def after_swap() -> None:
        calls.append((destination.read_bytes() == b"gdaemon-new", _store_lock_is_free(bin_dir)))

    promotion.stage_and_promote_binary_file(
        source, destination=destination, identity=_identity("2.0.0"), after_swap=after_swap
    )

    assert calls == [(True, False)]


def test_generation_captured_during_after_swap_never_pairs_new_bytes_with_old_sidecar(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    version_file = _write_bytes(bin_dir / ".gdaemon-version", b"1.0.0\n", mode=0o644)
    _write_bytes(bin_dir / "gdaemon", b"gdaemon-v1")
    installing = _write_bytes(tmp_path / "source" / "v2" / "gdaemon", b"gdaemon-v2")
    racing = _write_bytes(tmp_path / "source" / "v3" / "gdaemon", b"gdaemon-v3")
    racer_errors: list[Exception] = []
    started: list[threading.Thread] = []

    def second_promotion() -> None:
        try:
            promotion.stage_and_promote_binary_file(
                racing, destination=bin_dir / "gdaemon", identity=_identity("3.0.0")
            )
        except Exception as exc:
            racer_errors.append(exc)

    racer = threading.Thread(target=second_promotion)

    def after_swap() -> None:
        # The sidecar write that belongs to the new binary, then a second promotion that
        # starts while this hook is still running and so must capture only after it.
        version_file.write_bytes(b"2.0.0\n")
        started.append(racer)
        racer.start()

    promotion.stage_and_promote_binary_file(
        installing,
        destination=bin_dir / "gdaemon",
        identity=_identity("2.0.0"),
        after_swap=after_swap,
    )

    assert len(started) == 1
    racer.join(timeout=30)
    assert not racer.is_alive()
    assert racer_errors == []
    holding_new_bytes = [
        generation
        for generation in list_generations(bin_dir)
        if (generation.path / "gdaemon").read_bytes() == b"gdaemon-v2"
    ]
    assert holding_new_bytes, "the second promotion captured no generation holding the new bytes"
    for generation in holding_new_bytes:
        assert (generation.path / ".gdaemon-version").read_bytes() == b"2.0.0\n"


def test_single_binary_after_swap_error_propagates_and_releases_the_store_lock(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    source = _write_bytes(tmp_path / "source" / "gdaemon", b"gdaemon-new")

    def after_swap() -> None:
        raise RuntimeError("sidecar write failed")

    with pytest.raises(RuntimeError, match="^sidecar write failed$"):
        promotion.stage_and_promote_binary_file(
            source,
            destination=bin_dir / "gdaemon",
            identity=_identity("2.0.0"),
            after_swap=after_swap,
        )

    assert _store_lock_is_free(bin_dir)


def test_workspace_set_after_swap_error_is_not_swallowed(workspace: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    candidates = {name: _member_stub(tmp_path / "1.0.0" / name, "1.0.0") for name in SET_MEMBERS}
    hook_error = RuntimeError("sidecar write failed")

    def after_swap() -> None:
        raise hook_error

    with pytest.raises(BinarySetCoherenceError, match="sidecar write failed") as raised:
        promote_workspace_binary_set(
            candidates, bin_dir=bin_dir, workspace_root=workspace, after_swap=after_swap
        )

    assert raised.value.__cause__ is hook_error
    assert _store_lock_is_free(bin_dir)
