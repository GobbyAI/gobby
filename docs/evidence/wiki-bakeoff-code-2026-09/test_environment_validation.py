"""Regression checks for evidence trust boundaries, without live services."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from runtime_boundary import file_record, write_once
from validate_environment import (
    _validate_grant_binding,
    _validate_installation_checks,
    validate_external_changes,
)


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
    prefix = str(Path.home() / ".gobby/worktrees/game-goblins/task-")
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
