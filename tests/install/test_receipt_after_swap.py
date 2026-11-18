"""A release receipt write that fails after the swap must not misreport the promoted member.

The swap replaces the installed binary before its release receipt is written. These tests hold
the set and restore paths to one behaviour when that receipt write fails after the replace: the
member counts as promoted, no receipt describing the outgoing version is left beside the new
bytes, and a retry of the same build is accepted rather than refused.

Each managed binary is a shell script that answers --version and the schema identity probe, as
the restore tests do. The workspace is a synthetic git checkout under pytest's temporary
directory, so release preflight never reads the real repository.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from gobby.install import bin_freshness_promotion as promotion
from gobby.install.bin_generations import Generation, retain_outgoing
from gobby.install.bin_release_identity import BinaryReleaseIdentity
from gobby.install.bin_set_coherence import (
    BinarySetCoherenceError,
    promote_workspace_binary_set,
)

if TYPE_CHECKING:
    from gobby.install.bin_restore import RestoreOutcome

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
SET_MEMBERS = ("gcode", "gdaemon", "ghook")
CRATE_DIRS = ("gcode", "gdaemon", "ghook", "gclient", "gmcp", "gterminal", "gcore")
PIN_NAME = ".gdaemon-schema-identity.json"
RECEIPT = ".gdaemon-release-identity.json"
IDENTITY: dict[str, int | str] = {
    "runner_protocol": 1,
    "baseline_version": 420,
    "baseline_checksum": "baseline",
    "latest_version": 468,
    "latest_checksum": "latest",
    "assets_root_hash": "root",
}
ReceiptWriter = Callable[[Path, str, BinaryReleaseIdentity], None]


class _Daemon:
    """Recording stand-ins for the daemon stop, start and restart check."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def stop(self) -> bool:
        self.events.append("stop")
        return True

    def start(self) -> None:
        self.events.append("start")

    def start_refusal(self, candidate: Path | None) -> str | None:
        self.events.append("start_refusal")
        return None

    def verify(self, bin_dir: Path) -> None:
        self.events.append("verify")


@pytest.fixture(autouse=True)
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Gobby-shaped checkout, so release preflight never reads the real one."""
    root = tmp_path / "checkout"
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
    monkeypatch.setattr(promotion, "_workspace_crates_root", lambda: root / "crates")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


def _at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def _write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _script(name: str, version: str, marker: str) -> bytes:
    """A managed binary reporting version for --version and the shared schema identity otherwise.

    The marker is a comment, so two builds at one version still have different bytes.
    """
    identity = json.dumps(IDENTITY, separators=(",", ":"), sort_keys=True)
    return (
        "#!/bin/sh\n"
        f"# build {marker}\n"
        'if [ "$1" = "--version" ]; then\n'
        f"printf '%s\\n' '{name} {version}'\n"
        "else\n"
        f"printf '%s\\n' '{identity}'\n"
        "fi\n"
    ).encode()


def _seed_installed_gdaemon(bin_dir: Path) -> None:
    """An installed gdaemon 1.0.0 with the receipt a promotion of it left behind.

    Set members write no raw source-hash stamp, so none is seeded: a retry then takes the
    legacy comparison, as a real set member does.
    """
    payload = _script("gdaemon", "1.0.0", "old")
    _write(bin_dir / "gdaemon", payload, 0o755)
    receipt = {
        "artifact_sha256": _sha(payload),
        "source_identity": "source-gdaemon-old",
        "version": "1.0.0",
    }
    _write(bin_dir / RECEIPT, _json_bytes(receipt), 0o600)


def _candidates(source_dir: Path, version: str, marker: str) -> dict[str, Path]:
    """One complete set of workspace builds, every member reporting version."""
    candidates: dict[str, Path] = {}
    for name in SET_MEMBERS:
        source = source_dir / name
        _write(source, _script(name, version, marker), 0o755)
        candidates[name] = source
    return candidates


def _receipt_write_fails_once(real: ReceiptWriter) -> ReceiptWriter:
    """A receipt writer whose first gdaemon write raises OSError after the swap has replaced it."""
    failed: list[bool] = []

    def write(bin_dir: Path, name: str, identity: BinaryReleaseIdentity) -> None:
        if name == "gdaemon" and not failed:
            failed.append(True)
            raise OSError("synthetic receipt write failure")
        real(bin_dir, name, identity)

    return write


def _place(bin_dir: Path, name: str, payload: bytes, receipt_version: str) -> None:
    """Install a binary with the receipts and sidecars a real promotion leaves beside it."""
    _write(bin_dir / name, payload, 0o755)
    receipt = {
        "artifact_sha256": _sha(payload),
        "source_identity": f"source-{name}",
        "version": receipt_version,
    }
    _write(bin_dir / f".{name}-release-identity.json", _json_bytes(receipt), 0o600)
    _write(bin_dir / f".{name}-version", f"{receipt_version}\n".encode(), 0o644)
    _write(
        bin_dir / f".{name}-install.json",
        _json_bytes({"installed_version": receipt_version}),
        0o644,
    )
    _write(bin_dir / f".{name}-source-sha256", f"{_sha(payload)}\n".encode(), 0o644)


def _install(bin_dir: Path, name: str, version: str, *, marker: str) -> None:
    _place(bin_dir, name, _script(name, version, marker), version)


def _write_stamp(bin_dir: Path) -> None:
    _write(bin_dir / PIN_NAME, _json_bytes(IDENTITY), 0o644)


def _retain(bin_dir: Path, members: list[str], minute: int) -> Generation:
    generation = retain_outgoing(bin_dir, members, now=_at(minute))
    assert generation is not None
    return generation


def _outgoing_store(tmp_path: Path) -> tuple[Path, Generation]:
    """A bin dir whose retained gdaemon is 1.0.0 and whose installed gdaemon is 1.0.1."""
    bin_dir = tmp_path / "bin"
    _write_stamp(bin_dir)
    _install(bin_dir, "gdaemon", "1.0.0", marker="old")
    generation = _retain(bin_dir, ["gdaemon"], 0)
    _install(bin_dir, "gdaemon", "1.0.1", marker="new")
    return bin_dir, generation


def _restore(
    bin_dir: Path,
    generation: Generation,
    members: list[str],
    daemon: _Daemon,
) -> RestoreOutcome:
    from gobby.install.bin_restore import run_restore

    return run_restore(
        bin_dir,
        generation,
        members,
        hold_token=None,
        stop_daemon=daemon.stop,
        start_daemon=daemon.start,
        start_refusal=daemon.start_refusal,
        verify_restart_target=daemon.verify,
        now=_at(10),
    )


def test_set_promotion_counts_gdaemon_promoted_when_its_receipt_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    _seed_installed_gdaemon(bin_dir)
    candidates = _candidates(source_dir, "1.0.1", "new")
    monkeypatch.setattr(
        promotion,
        "write_release_identity",
        _receipt_write_fails_once(promotion.write_release_identity),
    )

    # Before the fix this raises BinarySetCoherenceError listing gdaemon as unpromoted.
    promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    assert (bin_dir / "gdaemon").read_bytes() == candidates["gdaemon"].read_bytes()
    assert not (bin_dir / RECEIPT).exists()


def test_retry_of_the_same_build_is_accepted_after_its_receipt_write_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    _seed_installed_gdaemon(bin_dir)
    candidates = _candidates(source_dir, "1.0.1", "new")
    monkeypatch.setattr(
        promotion,
        "write_release_identity",
        _receipt_write_fails_once(promotion.write_release_identity),
    )

    # The first attempt may raise before the fix; the swap it made is what the retry starts from.
    with contextlib.suppress(BinarySetCoherenceError):
        promote_workspace_binary_set(candidates, bin_dir=bin_dir)
    assert (bin_dir / "gdaemon").read_bytes() == candidates["gdaemon"].read_bytes()

    # Before the fix this raises BinaryReleaseVersionError: "gdaemon: changed or unverified
    # source under installed version 1.0.1; bump its patch version before promotion".
    promote_workspace_binary_set(candidates, bin_dir=bin_dir)

    assert (bin_dir / "gdaemon").read_bytes() == candidates["gdaemon"].read_bytes()


def test_restore_completes_when_the_receipt_write_fails_after_the_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir, generation = _outgoing_store(tmp_path)
    monkeypatch.setattr(
        promotion,
        "write_release_identity",
        _receipt_write_fails_once(promotion.write_release_identity),
    )
    daemon = _Daemon()

    # Before the fix this raises RestoreError: "replaced: none; not replaced: gdaemon".
    outcome = _restore(bin_dir, generation, ["gdaemon"], daemon)

    assert outcome.restored == {"gdaemon": "1.0.0"}
    assert outcome.burned == {"gdaemon": "1.0.1"}
