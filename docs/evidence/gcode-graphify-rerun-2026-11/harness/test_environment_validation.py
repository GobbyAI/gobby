"""Regression checks for evidence trust boundaries, without live services."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from provision_environment import GOBBY_SHA
from runtime_boundary import file_record, write_once
from validate_environment import (
    _validate_grant_binding,
    _validate_installation_checks,
    _validate_source_manifest,
    validate_external_changes,
    validate_gcode_receipt,
    validate_graphify_receipt,
)

SOURCE_PIN = "9b148b644c63a8ac2a28f341ecac585210ecd3fe"


@pytest.fixture
def grant(tmp_path: Path) -> dict[str, Any]:
    write_once(tmp_path / "corpora/gcode/C0/.gobby/project.json", '{"id":"owned"}', 0o600)
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
                "dsn": "host=127.0.0.1 port=61234 dbname=gobby_bakeoff_21942 "
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
    with pytest.raises(AssertionError):
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
    with pytest.raises(AssertionError, match="artifact hash changed"):
        _validate_installation_checks(tmp_path, checks)


@pytest.fixture
def external_snapshots(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config_path = str(Path.home() / ".codex/config.toml")
    prefix = str(Path.home() / ".gobby/worktrees/gobby/wt-")
    old, new = prefix + "158-example", prefix + "162-example"
    before_text = f'model = "unchanged"\n[projects."{old}"]\ntrust_level = "trusted"\n'
    after_text = before_text.replace(old, new)
    capture = tmp_path / "isolation/codex.toml"
    write_once(capture, after_text, 0o600)
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
    base_file = {"path": config_path, "bytes": len(before_text.encode()), "mode": "0600"}
    before = {
        "source": source,
        "stable_global_files": [
            {**base_file, "sha256": hashlib.sha256(before_text.encode()).hexdigest()}
        ],
    }
    after = {
        "source": later,
        "stable_global_files": [
            {**base_file, "sha256": hashlib.sha256(after_text.encode()).hexdigest()}
        ],
    }
    attribution = {
        "source": {
            "before": source,
            "after": later,
            "owner_session": "coordinator",
            "message": "Owner attributed merges",
            "history": file_record(tmp_path, history),
        },
        "codex_trust": {
            "path": config_path,
            "owner_session": "coordinator",
            "message": "Owner confirmed spawn",
            "old_path": old,
            "new_path": new,
            "capture": file_record(tmp_path, capture),
        },
    }
    write_once(tmp_path / "isolation/concurrent-changes.json", json.dumps(attribution), 0o600)
    return before, after


def test_isolation_accepts_exact_attributed_trust_replacement(
    tmp_path: Path, external_snapshots: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    original = copy.deepcopy(external_snapshots)
    validate_external_changes(tmp_path, *external_snapshots)
    assert external_snapshots == original


def test_isolation_rejects_additional_global_change(
    tmp_path: Path, external_snapshots: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    before, after = external_snapshots
    before["stable_global_files"].append({"path": "/global/other", "sha256": "before"})
    after["stable_global_files"].append({"path": "/global/other", "sha256": "after"})
    with pytest.raises(AssertionError, match="unattributed global file change"):
        validate_external_changes(tmp_path, before, after)


def test_isolation_rejects_unrelated_config_edit_despite_matching_capture_hash(
    tmp_path: Path, external_snapshots: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    before, after = external_snapshots
    capture = tmp_path / "isolation/codex.toml"
    capture.write_text(capture.read_text().replace("unchanged", "different"))
    proof_path = tmp_path / "isolation/concurrent-changes.json"
    proof = json.loads(proof_path.read_text())
    proof["codex_trust"]["capture"] = file_record(tmp_path, capture)
    proof_path.write_text(json.dumps(proof))
    after["stable_global_files"][0]["sha256"] = hashlib.sha256(capture.read_bytes()).hexdigest()
    with pytest.raises(AssertionError, match="exceeds the attributed"):
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
    _validate_source_manifest(tmp_path, "gobby", SOURCE_PIN, record)


def test_source_manifest_rejects_symlink_escaping_source(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    source = tmp_path / "sources/gobby"
    source.mkdir(parents=True)
    (source / "leak.txt").symlink_to(outside)
    record = _write_source_manifest(tmp_path, [_symlink_item("leak.txt", str(outside))])
    with pytest.raises(AssertionError, match="escapes source root"):
        _validate_source_manifest(tmp_path, "gobby", SOURCE_PIN, record)


def test_source_manifest_rejects_retargeted_symlink(tmp_path: Path) -> None:
    source = tmp_path / "sources/gobby"
    source.mkdir(parents=True)
    (source / "AGENTS.md").write_text("rules\n")
    (source / "OTHER.md").write_text("other\n")
    (source / "CLAUDE.md").symlink_to("AGENTS.md")
    record = _write_source_manifest(
        tmp_path, [_symlink_item("CLAUDE.md", "AGENTS.md")]
    )
    (source / "CLAUDE.md").unlink()
    (source / "CLAUDE.md").symlink_to("OTHER.md")
    with pytest.raises(AssertionError, match="symlink target changed"):
        _validate_source_manifest(tmp_path, "gobby", SOURCE_PIN, record)


GRAPHIFY_VERSION = "0.10.0"
GCODE_CONTRACT = 8


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
    validate_graphify_receipt(tmp_path, graphify_receipt)


def test_graphify_receipt_rejects_version_absent_from_dependency_lock(
    tmp_path: Path, graphify_receipt: dict[str, Any]
) -> None:
    graphify_receipt["pypi_version"] = "0.10.1"
    with pytest.raises(AssertionError, match="absent from dependency lock"):
        validate_graphify_receipt(tmp_path, graphify_receipt)


def test_graphify_receipt_rejects_unexpected_entrypoint(
    tmp_path: Path, graphify_receipt: dict[str, Any]
) -> None:
    other = tmp_path / "tools/graphify/venv/bin/other"
    write_once(other, "other-binary\n", 0o755)
    graphify_receipt["entrypoints"] = [file_record(tmp_path, other)]
    with pytest.raises(AssertionError, match="unexpected graphify entrypoint"):
        validate_graphify_receipt(tmp_path, graphify_receipt)


def test_graphify_receipt_rejects_tampered_executable(
    tmp_path: Path, graphify_receipt: dict[str, Any]
) -> None:
    (tmp_path / "tools/graphify/venv/bin/graphify").write_text("graphify-hacked\n")
    with pytest.raises(AssertionError, match="artifact hash changed"):
        validate_graphify_receipt(tmp_path, graphify_receipt)


@pytest.fixture
def gcode_receipt(tmp_path: Path) -> dict[str, Any]:
    binary = tmp_path / "tools/gcode/bin/gcode"
    write_once(binary, "gcode-binary\n", 0o755)
    write_once(tmp_path / "gobby-home/bin/gdaemon", "gdaemon-binary\n", 0o755)
    write_once(tmp_path / "sources/gobby/Cargo.lock", "# lock\n", 0o644)
    manifest = _write_source_manifest(
        tmp_path, [_file_item(tmp_path, "Cargo.lock")], commit=GOBBY_SHA
    )
    version = _passed_check(
        tmp_path, [str(binary), "--version"], "probe-gcode-version", "gcode 1.7.0\n"
    )
    contract = _passed_check(
        tmp_path,
        [str(binary), "contract"],
        "probe-gcode-contract",
        json.dumps({"tool": "gcode", "contract_version": GCODE_CONTRACT}),
    )
    return {
        "source_commit": GOBBY_SHA,
        "version": "1.7.0",
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
    validate_gcode_receipt(tmp_path, gcode_receipt)


def test_gcode_receipt_rejects_contract_version_drift(
    tmp_path: Path, gcode_receipt: dict[str, Any]
) -> None:
    gcode_receipt["contract_version"] = 7
    with pytest.raises(AssertionError, match="contract"):
        validate_gcode_receipt(tmp_path, gcode_receipt)


def test_gcode_receipt_rejects_source_commit_other_than_gobby_pin(
    tmp_path: Path, gcode_receipt: dict[str, Any]
) -> None:
    gcode_receipt["source_commit"] = "0" * 40
    with pytest.raises(AssertionError, match="source_commit"):
        validate_gcode_receipt(tmp_path, gcode_receipt)
