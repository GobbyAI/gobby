"""The bearer probe builds from the pinned tree plus one recorded overlay, inside the runtime root."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from provision_environment import (
    GOBBY_SHA,
    PROBE_BINARY,
    PROBE_MANIFEST,
    PROBE_NAME,
    PROBE_SOURCE,
    PROBE_TARGET,
    build_probe,
)
from runtime_boundary import EvidenceError, file_record, verify_file_record

PROBE_BYTES = b"fn main() { /* managed bearer probe */ }\n"
BINARY_BYTES = b"\x7fELF managed bearer probe"


class FakeCargo:
    """Stands in for cargo: writes the artifact under the --target-dir the build names."""

    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, cwd))
        if self.returncode == 0:
            target = Path(argv[argv.index("--target-dir") + 1])
            artifact = target / "debug" / "examples" / PROBE_NAME
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(BINARY_BYTES)
        return subprocess.CompletedProcess(argv, self.returncode, stdout=b"", stderr=self.stderr)


def _runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "sources/gobby/crates/gcore/examples").mkdir(parents=True)
    (root / "manifests").mkdir(parents=True)
    return root


def _probe_source(tmp_path: Path) -> Path:
    source = tmp_path / "candidate" / f"{PROBE_NAME}.rs"
    source.parent.mkdir(parents=True)
    source.write_bytes(PROBE_BYTES)
    return source


def test_build_records_the_example_source_and_binary_hashes_in_the_manifest(
    tmp_path: Path,
) -> None:
    root = _runtime(tmp_path)

    build_probe(root, _probe_source(tmp_path), FakeCargo())

    manifest = json.loads((root / PROBE_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["pinned_commit"] == GOBBY_SHA
    assert manifest["example_in_pin"] is False
    assert manifest["example_source"] == file_record(root, f"sources/gobby/{PROBE_SOURCE}")
    assert manifest["example_source"]["sha256"] == hashlib.sha256(PROBE_BYTES).hexdigest()
    assert manifest["binary"] == file_record(root, PROBE_BINARY)
    assert manifest["binary"]["sha256"] == hashlib.sha256(BINARY_BYTES).hexdigest()
    verify_file_record(root, manifest["binary"])


def test_build_compiles_the_pinned_tree_into_the_runtime_target_dir(tmp_path: Path) -> None:
    root = _runtime(tmp_path)
    cargo = FakeCargo()

    build_probe(root, _probe_source(tmp_path), cargo)

    [(argv, cwd)] = cargo.calls
    assert argv == [
        "cargo",
        "build",
        "--locked",
        "--target-dir",
        str(root / PROBE_TARGET),
        "-p",
        "gobby-core",
        "--example",
        PROBE_NAME,
    ]
    assert cwd == root / "sources/gobby"


def test_build_never_names_the_operator_bin_in_its_command(tmp_path: Path) -> None:
    root = _runtime(tmp_path)
    cargo = FakeCargo()

    build_probe(root, _probe_source(tmp_path), cargo)

    operator_bin = str(Path.home() / ".gobby" / "bin")
    [(argv, cwd)] = cargo.calls
    assert operator_bin not in " ".join([*argv, str(cwd)])
    assert (root / PROBE_BINARY).read_bytes() == BINARY_BYTES


def test_build_refuses_a_pin_that_already_contains_the_example(tmp_path: Path) -> None:
    root = _runtime(tmp_path)
    pinned = root / "sources/gobby" / PROBE_SOURCE
    pinned.write_bytes(b"pinned example")
    cargo = FakeCargo()

    with pytest.raises(EvidenceError, match="already contains"):
        build_probe(root, _probe_source(tmp_path), cargo)
    assert cargo.calls == []


def test_build_refuses_to_overwrite_an_existing_probe(tmp_path: Path) -> None:
    root = _runtime(tmp_path)
    (root / PROBE_BINARY).parent.mkdir(parents=True)
    (root / PROBE_BINARY).write_bytes(b"older probe")
    cargo = FakeCargo()

    with pytest.raises(EvidenceError, match="refusing to overwrite"):
        build_probe(root, _probe_source(tmp_path), cargo)
    assert cargo.calls == []


def test_build_stops_with_cargo_stderr_and_records_nothing_when_the_compile_fails(
    tmp_path: Path,
) -> None:
    root = _runtime(tmp_path)
    cargo = FakeCargo(returncode=101, stderr=b"error[E0432]: unresolved import")

    with pytest.raises(EvidenceError, match="unresolved import"):
        build_probe(root, _probe_source(tmp_path), cargo)
    assert not (root / PROBE_MANIFEST).exists()
    assert not (root / PROBE_BINARY).exists()
