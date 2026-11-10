"""Regression checks for evidence trust boundaries, without live services."""

from __future__ import annotations

import builtins
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import record_installations
from provision_environment import (
    C0_WORKSPACE,
    GOBBY_SHA,
    PROBE_BINARY,
    PROBE_MANIFEST,
    PROBE_SOURCE,
    build_probe,
    external_state_snapshot,
)
from runtime_boundary import EvidenceError, file_record, write_once
from validate_environment import (
    AUTH_PROBE,
    _validate_grant_binding,
    _validate_installation_checks,
    _validate_source_manifest,
    auth_probe,
    bearer_probe_provenance,
    bearer_verdicts,
    validate_external_changes,
    validate_gcode_receipt,
    validate_graphify_receipt,
)

SOURCE_PIN = "9b148b644c63a8ac2a28f341ecac585210ecd3fe"


@pytest.fixture
def grant(tmp_path: Path) -> dict[str, Any]:
    write_once(tmp_path / C0_WORKSPACE / ".gobby/project.json", '{"id":"owned"}', 0o600)
    write_once(tmp_path / "gobby-home/machine_id", "owned-machine\n", 0o600)
    return {
        "version": 2,
        "api_contract": 1,
        "issued_at": 100,
        "expires_at": 200,
        "principal": {
            "kind": "interactive",
            "machine_id": "owned-machine",
            "project_id": "owned",
            "session_id": None,
            "execution_id": None,
        },
        "capabilities": {
            "postgres": {
                "mode": "direct",
                "valid_until": 200,
                "role_name": "gobby_ix_test",
                "dsn": "host=127.0.0.1 port=61234 dbname=gobby_bakeoff_22948 "
                "user=gobby_ix_test password='a quoted test password'",
            },
            "qdrant": {"mode": "direct", "url": "http://127.0.0.1:61235"},
            "falkordb": {"mode": "direct", "host": "127.0.0.1", "port": 61237},
        },
    }


def test_grant_accepts_bound_identity_and_native_libpq_dsn(
    tmp_path: Path, grant: dict[str, Any]
) -> None:
    connection = _validate_grant_binding(tmp_path, grant, 150)
    assert connection["user"] == "gobby_ix_test"
    assert connection["password"] == "a quoted test password"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 1),
        ("api_contract", 8),
        ("issued_at", 151),
        ("expires_at", 150),
        ("principal.project_id", "foreign"),
        ("principal.machine_id", "foreign"),
        ("principal.session_id", "ambient-session"),
        ("capabilities.postgres.valid_until", 150),
        ("capabilities.postgres.role_name", "another-role"),
        ("capabilities.postgres.dsn", "host=127.0.0.1 port=60891 dbname=gobby user=gobby"),
        ("capabilities.qdrant.url", "http://127.0.0.1:6333"),
        ("capabilities.falkordb.port", 16379),
    ],
)
def test_grant_rejects_stale_foreign_or_shared_capabilities(
    tmp_path: Path, grant: dict[str, Any], field: str, value: Any
) -> None:
    target = grant
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    with pytest.raises(EvidenceError):
        _validate_grant_binding(tmp_path, grant, 150)


def test_installation_rejects_tampered_probe_log(tmp_path: Path) -> None:
    log = tmp_path / "probe.log"
    log.write_text("passed")
    checks = [
        {
            "argv": ["tool", "--version"],
            "cwd": str(tmp_path),
            "exit_code": 0,
            "log": file_record(tmp_path, log),
        }
    ]
    _validate_installation_checks(tmp_path, checks)
    log.write_text("failed")
    with pytest.raises(EvidenceError, match="artifact hash changed"):
        _validate_installation_checks(tmp_path, checks)


OWNER_SESSION = "7674aa11-70a3-40d5-91dd-1fe46140732b"


@pytest.fixture
def external_snapshots(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config_path = str(Path.home() / ".codex/config.toml")
    history = tmp_path / "isolation/history.log"
    write_once(history, "before-head\nafter-head\n", 0o600)
    source = {
        "repo": "/source",
        "head": "before-head",
        "index_sha256": "before-index",
        "status_porcelain_v1": "",
        "status_sha256": "clean",
    }
    later = {**source, "head": "after-head", "index_sha256": "after-index"}
    # config.toml is recorded by stat only: inode, size, mtime_ns and mode, never its bytes.
    config = {
        "path": config_path,
        "exists": True,
        "type": "file",
        "mode": "0600",
        "size": 58,
        "mtime_ns": 1,
        "inode": 9,
    }
    before = {"source": source, "stable_global_files": [dict(config)]}
    after = {"source": later, "stable_global_files": [dict(config)]}
    # The isolated PATH the daemon was launched with. It holds no provider CLI.
    isolated_bin = tmp_path / "isolated-bin"
    isolated_bin.mkdir()
    write_isolated_path(tmp_path, str(isolated_bin))
    attribution = {
        "source": {
            "before": source,
            "after": later,
            "owner_session": OWNER_SESSION,
            "message": "Owner attributed merges",
            "history": file_record(tmp_path, history),
        },
        "codex_trust": {
            "path": config_path,
            "codex": "absent",
            "reason": "no provider CLI on the isolated PATH; config.toml is stat-only",
            "owner_session": OWNER_SESSION,
            "message": "Owner recorded no Codex trust change",
        },
    }
    write_once(tmp_path / "isolation/concurrent-changes.json", json.dumps(attribution), 0o600)
    return before, after


def write_isolated_path(root: Path, path_value: str) -> None:
    """Write the daemon launch receipt and its active-launch index, as launch_daemon.py does."""
    receipt = root / "receipts/daemon-launch-attempt-1.json"
    write_once(receipt, json.dumps({"path": path_value}), 0o600)
    index = {"daemon": file_record(root, receipt)}
    write_once(root / "receipts/active-launches.json", json.dumps(index), 0o600)


def test_isolation_accepts_no_codex_record_with_unchanged_globals(
    tmp_path: Path, external_snapshots: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    original = copy.deepcopy(external_snapshots)
    validate_external_changes(tmp_path, *external_snapshots)
    assert external_snapshots == original


def test_isolation_refuses_no_codex_record_when_codex_is_on_the_recorded_path(
    tmp_path: Path, external_snapshots: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    codex = tmp_path / "isolated-bin/codex"  # the recorded PATH's directory
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)
    with pytest.raises(EvidenceError, match="provider CLI on the isolated PATH: codex"):
        validate_external_changes(tmp_path, *external_snapshots)


def test_isolation_rejects_additional_global_change(
    tmp_path: Path, external_snapshots: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    before, after = external_snapshots
    before["stable_global_files"].append({"path": "/global/other", "sha256": "before"})
    after["stable_global_files"].append({"path": "/global/other", "sha256": "after"})
    with pytest.raises(EvidenceError, match="unattributed global file change"):
        validate_external_changes(tmp_path, before, after)


def test_isolation_rejects_config_fingerprint_change_under_no_codex_record(
    tmp_path: Path, external_snapshots: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    before, after = external_snapshots
    after["stable_global_files"][0]["mtime_ns"] = 2
    with pytest.raises(EvidenceError, match="unattributed global file change"):
        validate_external_changes(tmp_path, before, after)


OPERATOR_PATHS = (
    ".gobby/bootstrap.yaml",
    ".gobby/.secret_kek",
    ".gobby/.secret_salt",
    ".gobby/local_cli_token",
    ".codex/auth.json",
    ".codex/config.toml",
)


def test_external_snapshot_never_opens_operator_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    for rel in (*OPERATOR_PATHS, ".gobby/machine_id"):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("fixture\n")
    operator = {str(home / rel) for rel in OPERATOR_PATHS}
    monkeypatch.setattr("provision_environment._git_status", lambda repo: {})
    monkeypatch.setattr("provision_environment._command_observation", lambda argv: {})

    def refuse_operator_files(real: Any) -> Any:
        def guarded(file: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(file, (str, os.PathLike)) and os.fsdecode(file) in operator:
                raise AssertionError(f"operator file opened: {file}")
            return real(file, *args, **kwargs)

        return guarded

    monkeypatch.setattr(builtins, "open", refuse_operator_files(builtins.open))
    monkeypatch.setattr(io, "open", refuse_operator_files(io.open))
    snapshot = external_state_snapshot(tmp_path)

    records = {item["path"]: item for item in snapshot["stable_global_files"]}
    for rel in OPERATOR_PATHS:
        record = records[str(home / rel)]
        assert record["exists"] is True
        assert {"type", "mode", "size", "mtime_ns", "inode"} <= record.keys()
        assert "sha256" not in record
    assert "sha256" in records[str(home / ".gobby/machine_id")]


def test_isolation_rejects_operator_stat_change(
    tmp_path: Path, external_snapshots: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    before, after = external_snapshots
    operator = {
        "path": "/home/.gobby/bootstrap.yaml",
        "exists": True,
        "type": "file",
        "mode": "0600",
        "size": 10,
        "mtime_ns": 1,
        "inode": 7,
    }
    before["stable_global_files"].append(operator)
    after["stable_global_files"].append({**operator, "mtime_ns": 2})
    with pytest.raises(EvidenceError, match="unattributed global file change"):
        validate_external_changes(tmp_path, before, after)


def _write_source_manifest(
    root: Path, files: list[dict[str, Any]], commit: str = SOURCE_PIN
) -> dict[str, Any]:
    manifest_path = root / "manifests/gobby-source.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_commit": commit,
        "root": str(root / "sources/gobby"),
        "file_count": len(files),
        "files": files,
    }
    manifest_path.write_text(json.dumps(manifest))
    return file_record(root, manifest_path)


def _file_item(root: Path, path: str) -> dict[str, Any]:
    record = file_record(root, root / "sources/gobby" / path)
    return {**record, "path": path, "type": "file", "mode": "0644"}


def _symlink_item(path: str, target: str) -> dict[str, Any]:
    payload = target.encode("utf-8")
    return {
        "path": path,
        "type": "symlink",
        "symlink": True,
        "target": target,
        "mode": "0777",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def test_source_manifest_accepts_symlink_contained_in_source(tmp_path: Path) -> None:
    vendor = tmp_path / "sources/gobby/crates/vendor"
    vendor.mkdir(parents=True)
    (vendor / "AGENTS.md").write_text("rules\n")
    (vendor / "CLAUDE.md").symlink_to("AGENTS.md")
    record = _write_source_manifest(
        tmp_path,
        [
            _file_item(tmp_path, "crates/vendor/AGENTS.md"),
            _symlink_item("crates/vendor/CLAUDE.md", "AGENTS.md"),
        ],
    )
    before = copy.deepcopy(record)
    _validate_source_manifest(tmp_path, "gobby", SOURCE_PIN, record)
    assert record == before


def test_source_manifest_rejects_symlink_escaping_source(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    source = tmp_path / "sources/gobby"
    source.mkdir(parents=True)
    (source / "leak.txt").symlink_to(outside)
    record = _write_source_manifest(tmp_path, [_symlink_item("leak.txt", str(outside))])
    with pytest.raises(EvidenceError, match="escapes source root"):
        _validate_source_manifest(tmp_path, "gobby", SOURCE_PIN, record)


def test_source_manifest_rejects_retargeted_symlink(tmp_path: Path) -> None:
    source = tmp_path / "sources/gobby"
    source.mkdir(parents=True)
    (source / "AGENTS.md").write_text("rules\n")
    (source / "OTHER.md").write_text("other\n")
    (source / "CLAUDE.md").symlink_to("AGENTS.md")
    record = _write_source_manifest(tmp_path, [_symlink_item("CLAUDE.md", "AGENTS.md")])
    (source / "CLAUDE.md").unlink()
    (source / "CLAUDE.md").symlink_to("OTHER.md")
    with pytest.raises(EvidenceError, match="symlink target changed"):
        _validate_source_manifest(tmp_path, "gobby", SOURCE_PIN, record)


GRAPHIFY_VERSION = "0.10.0"
GCODE_CONTRACT = 12


def _install_log(root: Path, name: str, text: str) -> dict[str, Any]:
    log = root / f"logs/install/{name}.log"
    write_once(log, text, 0o600)
    return file_record(root, log)


def _passed_check(root: Path, argv: list[str], name: str, text: str) -> dict[str, Any]:
    return {"argv": argv, "cwd": str(root), "exit_code": 0, "log": _install_log(root, name, text)}


@pytest.fixture
def graphify_receipt(tmp_path: Path) -> dict[str, Any]:
    binary = tmp_path / "tools/graphify/venv/bin/graphify"
    write_once(binary, "graphify-binary\n", 0o755)
    version = _passed_check(
        tmp_path,
        [str(binary), "--version"],
        "probe-graphify-version",
        f"graphify {GRAPHIFY_VERSION}\n",
    )
    dependencies = _passed_check(
        tmp_path,
        ["uv", "pip", "check"],
        "probe-graphify-dependencies",
        "All installed packages are compatible\n",
    )
    freeze = _install_log(tmp_path, "probe-graphify-freeze", f"graphifyy=={GRAPHIFY_VERSION}\n")
    return {
        "status": "installed",
        "pypi_package": "graphifyy",
        "pypi_version": GRAPHIFY_VERSION,
        "entrypoints": [file_record(tmp_path, binary)],
        "dependency_locks": [freeze],
        "checks": [version, dependencies],
        "version_log": version["log"],
        "evidence": [],
    }


def test_graphify_receipt_accepts_installed_pypi_version(
    tmp_path: Path, graphify_receipt: dict[str, Any]
) -> None:
    before = copy.deepcopy(graphify_receipt)
    validate_graphify_receipt(tmp_path, graphify_receipt)
    assert graphify_receipt == before


def test_graphify_receipt_rejects_version_absent_from_dependency_lock(
    tmp_path: Path, graphify_receipt: dict[str, Any]
) -> None:
    graphify_receipt["pypi_version"] = "0.10.1"
    with pytest.raises(EvidenceError, match="absent from dependency lock"):
        validate_graphify_receipt(tmp_path, graphify_receipt)


def test_graphify_receipt_rejects_unexpected_entrypoint(
    tmp_path: Path, graphify_receipt: dict[str, Any]
) -> None:
    other = tmp_path / "tools/graphify/venv/bin/other"
    write_once(other, "other-binary\n", 0o755)
    graphify_receipt["entrypoints"] = [file_record(tmp_path, other)]
    with pytest.raises(EvidenceError, match="unexpected graphify entrypoint"):
        validate_graphify_receipt(tmp_path, graphify_receipt)


def test_graphify_receipt_rejects_tampered_executable(
    tmp_path: Path, graphify_receipt: dict[str, Any]
) -> None:
    (tmp_path / "tools/graphify/venv/bin/graphify").write_text("graphify-hacked\n")
    with pytest.raises(EvidenceError, match="artifact hash changed"):
        validate_graphify_receipt(tmp_path, graphify_receipt)


@pytest.fixture
def gcode_receipt(tmp_path: Path) -> dict[str, Any]:
    binary = tmp_path / "gobby-home/bin/gcode"
    write_once(binary, "gcode-binary\n", 0o755)
    write_once(tmp_path / "gobby-home/bin/gdaemon", "gdaemon-binary\n", 0o755)
    write_once(tmp_path / "sources/gobby/Cargo.lock", "# lock\n", 0o644)
    write_once(
        tmp_path / "sources/gobby/crates/gcode/Cargo.toml",
        '[package]\nname = "gcode"\nversion = "1.9.105"\n',
        0o644,
    )
    manifest = _write_source_manifest(
        tmp_path, [_file_item(tmp_path, "Cargo.lock")], commit=GOBBY_SHA
    )
    version = _passed_check(
        tmp_path, [str(binary), "--version"], "probe-gcode-version", "gcode 1.9.105\n"
    )
    contract = _passed_check(
        tmp_path,
        [str(binary), "contract"],
        "probe-gcode-contract",
        json.dumps({"tool": "gcode", "contract_version": GCODE_CONTRACT}),
    )
    return {
        "source_commit": GOBBY_SHA,
        "version": "1.9.105",
        "contract_version": GCODE_CONTRACT,
        "executable": file_record(tmp_path, binary),
        "gdaemon": file_record(tmp_path, "gobby-home/bin/gdaemon"),
        "cargo_lock": file_record(tmp_path, "sources/gobby/Cargo.lock"),
        "source_manifest": manifest,
        "checks": [version, contract],
        "version_log": version["log"],
        "contract_log": contract["log"],
    }


def test_gcode_receipt_accepts_pinned_build(tmp_path: Path, gcode_receipt: dict[str, Any]) -> None:
    before = copy.deepcopy(gcode_receipt)
    validate_gcode_receipt(tmp_path, gcode_receipt)
    assert gcode_receipt == before


def test_gcode_receipt_rejects_contract_version_drift(
    tmp_path: Path, gcode_receipt: dict[str, Any]
) -> None:
    gcode_receipt["contract_version"] = 7
    with pytest.raises(EvidenceError, match="contract"):
        validate_gcode_receipt(tmp_path, gcode_receipt)


def test_gcode_receipt_rejects_the_previous_pin_as_its_version(
    tmp_path: Path, gcode_receipt: dict[str, Any]
) -> None:
    gcode_receipt["version"] = "1.9.104"
    with pytest.raises(EvidenceError, match="gcode version is not pinned"):
        validate_gcode_receipt(tmp_path, gcode_receipt)


def test_gcode_receipt_rejects_a_version_probe_from_the_previous_pin(
    tmp_path: Path, gcode_receipt: dict[str, Any]
) -> None:
    binary = tmp_path / "gobby-home/bin/gcode"
    old_probe = _passed_check(
        tmp_path, [str(binary), "--version"], "probe-gcode-version-old", "gcode 1.9.104\n"
    )
    gcode_receipt["checks"] = [old_probe, gcode_receipt["checks"][1]]
    gcode_receipt["version_log"] = old_probe["log"]
    with pytest.raises(EvidenceError, match="gcode version log mismatch"):
        validate_gcode_receipt(tmp_path, gcode_receipt)


def test_recorded_gcode_version_is_the_archived_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_once(
        tmp_path / "sources/gobby/crates/gcode/Cargo.toml",
        '[package]\nname = "gcode"\nversion = "1.9.105"\n',
        0o644,
    )
    recorded: dict[str, Any] = {}
    probed: list[list[str]] = []

    def fake_probe(
        root: Path,
        attempt: str,
        label: str,
        argv: list[str],
        cwd: Path,
        env: Any = None,
        wrap: Any = None,
    ) -> dict[str, Any]:
        probed.append(argv)
        return {"argv": argv, "cwd": str(cwd), "exit_code": 0, "log": {"label": label}}

    def capture_gcode(root: Path, gcode: dict[str, Any]) -> None:
        recorded.update(gcode)

    monkeypatch.setattr(record_installations, "DEFAULT_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(record_installations, "assert_owned_runtime", lambda *_args: None)
    monkeypatch.setattr(
        record_installations, "third_party_sandbox", lambda *_args: (lambda argv: argv, {})
    )
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(record_installations, "probe", fake_probe)
    monkeypatch.setattr(record_installations, "_pypi_version", lambda *_args: "0.9.103")
    monkeypatch.setattr(
        record_installations, "file_record", lambda _root, path: {"path": str(path)}
    )
    monkeypatch.setattr(record_installations, "validate_graphify_receipt", lambda *_args: None)
    monkeypatch.setattr(record_installations, "validate_gcode_receipt", capture_gcode)
    monkeypatch.setattr(
        sys, "argv", ["record_installations", "--owner-session", "owner", "--attempt", "rerun1"]
    )
    assert record_installations.main() == 0
    assert recorded["version"] == "1.9.105"
    assert [str(tmp_path / "gobby-home/bin/gcode"), "--version"] in probed
    assert [str(tmp_path / "gobby-home/bin/gcode"), "contract"] in probed


def test_graphify_rejection_survives_python_optimize(
    tmp_path: Path, graphify_receipt: dict[str, Any]
) -> None:
    # Under -O, assert statements are stripped. The rejection must still raise.
    graphify_receipt["pypi_version"] = "0.10.1"
    probe = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from runtime_boundary import EvidenceError\n"
        "from validate_environment import validate_graphify_receipt\n"
        "receipt = json.loads(sys.stdin.read())\n"
        "try:\n"
        "    validate_graphify_receipt(Path(sys.argv[1]), receipt)\n"
        "except EvidenceError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(3)\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", probe, str(tmp_path)],
        input=json.dumps(graphify_receipt),
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_gcode_receipt_rejects_source_commit_other_than_gobby_pin(
    tmp_path: Path, gcode_receipt: dict[str, Any]
) -> None:
    gcode_receipt["source_commit"] = "0" * 40
    with pytest.raises(EvidenceError, match="source_commit"):
        validate_gcode_receipt(tmp_path, gcode_receipt)


def test_auth_probe_needs_no_local_token_file_or_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    write_once(root / AUTH_PROBE, "built probe\n", 0o700)
    (root / "gobby-home").mkdir()  # no gobby-home/local_cli_token exists
    cache = root / "gobby-home/grants/deployment/project.json"
    write_once(cache, "{}", 0o600)
    argvs: list[list[str]] = []

    def run_probe(argv: list[str], *, env: dict[str, str] | None = None) -> str:
        argvs.append(argv)
        return '{"mode": "valid", "verdict": "ok", "config_revision": 7}'

    monkeypatch.setattr("validate_environment._run_probe", run_probe)
    verdict = auth_probe(root, cache, "valid")
    assert verdict == {"mode": "valid", "verdict": "ok", "config_revision": 7}
    assert len(argvs) == 1
    assert not any("local_cli_token" in arg or "Local-Token" in arg for arg in argvs[0])


def test_bearer_verdicts_record_the_live_ok_and_the_refused_tamper() -> None:
    valid = {"mode": "valid", "verdict": "ok", "config_revision": 7}
    tampered = {"mode": "tampered", "verdict": "invalid_signature"}

    assert bearer_verdicts(valid, tampered, 7) == {
        "valid_probe": {"verdict": "ok", "config_revision_match": True},
        "tampered_probe": {"verdict": "invalid_signature"},
    }


def test_bearer_verdicts_refuse_a_refusal_code_in_place_of_the_valid_ok() -> None:
    valid = {"mode": "valid", "verdict": "invalid_signature"}
    tampered = {"mode": "tampered", "verdict": "invalid_signature"}

    with pytest.raises(EvidenceError, match="valid bearer probe was refused"):
        bearer_verdicts(valid, tampered, 7)


def test_bearer_verdicts_refuse_a_config_revision_that_is_not_the_grants() -> None:
    valid = {"mode": "valid", "verdict": "ok", "config_revision": 6}
    tampered = {"mode": "tampered", "verdict": "invalid_signature"}

    with pytest.raises(EvidenceError, match="config_revision"):
        bearer_verdicts(valid, tampered, 7)


def test_bearer_verdicts_refuse_a_tampered_grant_that_the_daemon_accepts() -> None:
    valid = {"mode": "valid", "verdict": "ok", "config_revision": 7}
    tampered = {"mode": "tampered", "verdict": "ok", "config_revision": 7}

    with pytest.raises(EvidenceError, match="tampered bearer probe"):
        bearer_verdicts(valid, tampered, 7)


CANDIDATE = "a" * 40


def _fake_run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Passes the read-only candidate checks and writes the artifact cargo would build."""
    if argv[:3] == ["git", "diff", "--name-only"]:
        return subprocess.CompletedProcess(argv, 0, stdout=f"{PROBE_SOURCE}\n".encode(), stderr=b"")
    if argv[:3] == ["git", "diff", "--quiet"]:
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
    if argv[:2] == ["git", "show"]:
        return subprocess.CompletedProcess(argv, 0, stdout=b"fn main() {}\n", stderr=b"")
    artifact = Path(argv[argv.index("--target-dir") + 1]) / "debug/examples/managed_bearer_probe"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"\x7fELF bearer probe")
    return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")


def _built_probe(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "sources/gobby/crates/gcore/examples").mkdir(parents=True)
    (root / "manifests").mkdir(parents=True)
    source = tmp_path / "candidate.rs"
    source.write_bytes(b"fn main() {}\n")
    build_probe(root, source, CANDIDATE, Path("/operator/gobby-repo"), _fake_run)
    return root


def test_provenance_accepts_the_binary_and_source_the_build_recorded(tmp_path: Path) -> None:
    root = _built_probe(tmp_path)
    manifest = json.loads((root / PROBE_MANIFEST).read_text(encoding="utf-8"))

    provenance = bearer_probe_provenance(root)

    assert provenance["binary"] == file_record(root, AUTH_PROBE) == manifest["binary"]
    assert provenance["example_source"] == manifest["example_source"]


def test_provenance_refuses_a_probe_binary_replaced_after_the_build(tmp_path: Path) -> None:
    root = _built_probe(tmp_path)
    (root / PROBE_BINARY).write_bytes(b"replacement probe")

    with pytest.raises(EvidenceError, match="differs from the build record"):
        bearer_probe_provenance(root)


def test_provenance_refuses_an_example_source_changed_after_the_build(tmp_path: Path) -> None:
    root = _built_probe(tmp_path)
    example = root / "sources/gobby" / PROBE_SOURCE
    example.write_bytes(example.read_bytes().replace(b"main", b"mein"))

    with pytest.raises(EvidenceError, match="artifact hash changed"):
        bearer_probe_provenance(root)
