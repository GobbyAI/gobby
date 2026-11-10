"""The bearer probe builds from the pinned tree plus one recorded overlay, inside the runtime root."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

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
CANDIDATE = "de9af577c731f4f355d058c9e0992f6971455770"
GOBBY_REPO = Path("/operator/gobby-repo")
EXAMPLE_ONLY = f"{PROBE_SOURCE}\n".encode()
CHECK_CRATES = ["git", "diff", "--name-only", GOBBY_SHA, CANDIDATE, "--", "crates"]
CHECK_LOCK = ["git", "diff", "--quiet", GOBBY_SHA, CANDIDATE, "--", "Cargo.lock"]
CHECK_BLOB = ["git", "show", f"{CANDIDATE}:{PROBE_SOURCE}"]


class FakeRunner:
    """Stands in for cargo and the read-only git checks, and records every argv with its cwd.

    `git_error` names one git check (crates, lock or blob) that fails with exit 128.
    """

    def __init__(
        self,
        *,
        cargo_returncode: int = 0,
        cargo_stderr: bytes = b"",
        crates: bytes = EXAMPLE_ONLY,
        lock_differs: bool = False,
        candidate_blob: bytes = PROBE_BYTES,
        git_error: str | None = None,
    ) -> None:
        self.cargo_returncode = cargo_returncode
        self.cargo_stderr = cargo_stderr
        self.crates = crates
        self.lock_differs = lock_differs
        self.candidate_blob = candidate_blob
        self.git_error = git_error
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, cwd))
        if argv[0] == "git":
            return self._git(argv)
        return self._cargo(argv)

    def cargo_calls(self) -> list[tuple[list[str], Path]]:
        return [call for call in self.calls if call[0][0] == "cargo"]

    def _git(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        if argv[:3] == ["git", "diff", "--name-only"]:
            return self._result(argv, "crates", stdout=self.crates)
        if argv[:3] == ["git", "diff", "--quiet"]:
            return self._result(argv, "lock", returncode=1 if self.lock_differs else 0)
        if argv[:2] == ["git", "show"]:
            return self._result(argv, "blob", stdout=self.candidate_blob)
        raise AssertionError(f"unexpected git command: {argv}")

    def _result(
        self, argv: list[str], check: str, *, stdout: bytes = b"", returncode: int = 0
    ) -> subprocess.CompletedProcess[bytes]:
        if self.git_error == check:
            return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b"fatal: bad object")
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=b"")

    def _cargo(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        if self.cargo_returncode == 0:
            target = Path(argv[argv.index("--target-dir") + 1])
            artifact = target / "debug" / "examples" / PROBE_NAME
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(BINARY_BYTES)
        return subprocess.CompletedProcess(
            argv, self.cargo_returncode, stdout=b"", stderr=self.cargo_stderr
        )


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


def _build(
    root: Path, tmp_path: Path, runner: FakeRunner, candidate: str = CANDIDATE
) -> dict[str, Any]:
    return build_probe(root, _probe_source(tmp_path), candidate, GOBBY_REPO, runner)


def _assert_nothing_recorded(root: Path, runner: FakeRunner) -> None:
    assert runner.cargo_calls() == []
    assert not (root / "sources/gobby" / PROBE_SOURCE).exists()
    assert not (root / PROBE_BINARY).exists()
    assert not (root / PROBE_MANIFEST).exists()


def test_build_records_the_candidate_and_both_example_hashes_in_the_manifest(
    tmp_path: Path,
) -> None:
    root = _runtime(tmp_path)

    _build(root, tmp_path, FakeRunner())

    manifest = json.loads((root / PROBE_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["pinned_commit"] == GOBBY_SHA
    assert manifest["example_in_pin"] is False
    assert manifest["candidate_commit"] == CANDIDATE
    assert manifest["candidate_blob_sha256"] == hashlib.sha256(PROBE_BYTES).hexdigest()
    assert manifest["archived_copy_sha256"] == hashlib.sha256(PROBE_BYTES).hexdigest()
    assert manifest["example_source"] == file_record(root, f"sources/gobby/{PROBE_SOURCE}")
    assert manifest["example_source"]["sha256"] == manifest["archived_copy_sha256"]
    assert manifest["binary"] == file_record(root, PROBE_BINARY)
    assert manifest["binary"]["sha256"] == hashlib.sha256(BINARY_BYTES).hexdigest()
    verify_file_record(root, manifest["binary"])


def test_build_checks_the_candidate_with_read_only_git_in_the_gobby_repo(
    tmp_path: Path,
) -> None:
    root = _runtime(tmp_path)
    runner = FakeRunner()

    _build(root, tmp_path, runner)

    assert runner.calls[:3] == [
        (CHECK_CRATES, GOBBY_REPO),
        (CHECK_LOCK, GOBBY_REPO),
        (CHECK_BLOB, GOBBY_REPO),
    ]
    assert [argv[0] for argv, _ in runner.calls[3:]] == ["cargo"]


def test_build_refuses_a_probe_source_that_differs_from_the_candidate_blob(
    tmp_path: Path,
) -> None:
    root = _runtime(tmp_path)
    runner = FakeRunner(candidate_blob=b"fn main() { /* another tree */ }\n")

    with pytest.raises(EvidenceError, match="differs from the candidate"):
        _build(root, tmp_path, runner)
    _assert_nothing_recorded(root, runner)


@pytest.mark.parametrize(
    "crates",
    [b"", f"{PROBE_SOURCE}\ncrates/gcore/src/lib.rs\n".encode()],
    ids=["example-missing", "extra-crate-file"],
)
def test_build_refuses_a_candidate_that_does_not_change_exactly_the_example(
    tmp_path: Path, crates: bytes
) -> None:
    root = _runtime(tmp_path)
    runner = FakeRunner(crates=crates)

    with pytest.raises(EvidenceError, match="crates"):
        _build(root, tmp_path, runner)
    _assert_nothing_recorded(root, runner)


def test_build_refuses_a_candidate_whose_cargo_lock_differs_from_the_pin(
    tmp_path: Path,
) -> None:
    root = _runtime(tmp_path)
    runner = FakeRunner(lock_differs=True)

    with pytest.raises(EvidenceError, match="Cargo.lock"):
        _build(root, tmp_path, runner)
    _assert_nothing_recorded(root, runner)


@pytest.mark.parametrize("git_error", ["crates", "lock", "blob"])
def test_build_fails_closed_when_a_git_check_cannot_run(tmp_path: Path, git_error: str) -> None:
    root = _runtime(tmp_path)
    runner = FakeRunner(git_error=git_error)

    with pytest.raises(EvidenceError):
        _build(root, tmp_path, runner)
    _assert_nothing_recorded(root, runner)


@pytest.mark.parametrize("candidate", ["de9af577", "g" * 40])
def test_build_refuses_a_candidate_that_is_not_a_full_commit_sha(
    tmp_path: Path, candidate: str
) -> None:
    root = _runtime(tmp_path)
    runner = FakeRunner()

    with pytest.raises(EvidenceError, match="full commit SHA"):
        _build(root, tmp_path, runner, candidate=candidate)
    assert runner.calls == []


def test_build_compiles_the_pinned_tree_into_the_runtime_target_dir(tmp_path: Path) -> None:
    root = _runtime(tmp_path)
    runner = FakeRunner()

    _build(root, tmp_path, runner)

    [(argv, cwd)] = runner.cargo_calls()
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
    runner = FakeRunner()

    _build(root, tmp_path, runner)

    operator_bin = str(Path.home() / ".gobby" / "bin")
    [(argv, cwd)] = runner.cargo_calls()
    assert operator_bin not in " ".join([*argv, str(cwd)])
    assert (root / PROBE_BINARY).read_bytes() == BINARY_BYTES


def test_build_refuses_a_pin_that_already_contains_the_example(tmp_path: Path) -> None:
    root = _runtime(tmp_path)
    pinned = root / "sources/gobby" / PROBE_SOURCE
    pinned.write_bytes(b"pinned example")
    runner = FakeRunner()

    with pytest.raises(EvidenceError, match="already contains"):
        _build(root, tmp_path, runner)
    assert runner.calls == []


def test_build_refuses_to_overwrite_an_existing_probe(tmp_path: Path) -> None:
    root = _runtime(tmp_path)
    (root / PROBE_BINARY).parent.mkdir(parents=True)
    (root / PROBE_BINARY).write_bytes(b"older probe")
    runner = FakeRunner()

    with pytest.raises(EvidenceError, match="refusing to overwrite"):
        _build(root, tmp_path, runner)
    assert runner.calls == []


def test_build_stops_with_cargo_stderr_and_records_nothing_when_the_compile_fails(
    tmp_path: Path,
) -> None:
    root = _runtime(tmp_path)
    runner = FakeRunner(cargo_returncode=101, cargo_stderr=b"error[E0432]: unresolved import")

    with pytest.raises(EvidenceError, match="unresolved import"):
        _build(root, tmp_path, runner)
    assert not (root / PROBE_MANIFEST).exists()
    assert not (root / PROBE_BINARY).exists()
