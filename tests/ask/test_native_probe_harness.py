from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from gobby.ask.runtime_validation import (
    ASK_NATIVE_PROBE_EXPECTATIONS,
    ASK_SRT_POLICY_SCHEMA_VERSION,
    AskRuntimeValidationArtifact,
    ask_runtime_control_digest,
    load_ask_runtime_validation,
    load_ask_runtime_validation_artifacts,
)
from tests.ask import native_probe_harness as harness

pytestmark = pytest.mark.unit

_TEST_DATABASE_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
_SCHEMA = "gobby_test_askprobe_abcdef"
_SCOPED_TEST_DATABASE_URL = _TEST_DATABASE_URL + "?options=-csearch_path%3D" + _SCHEMA


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _provider(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf '2.1.265\\n'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _policy_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, str]]:
    source_root = str((tmp_path / "source").resolve())
    scratch_root = str((tmp_path / "scratch").resolve())
    run_root = (tmp_path / "run").resolve()
    policy_path = str(run_root / "assets" / "settings.json")
    run_tmp_root = str(Path(tempfile.gettempdir()).resolve() / "gobby-12345678")
    policy: dict[str, object] = {
        "network": {
            "allowedDomains": [],
            "deniedDomains": ["*"],
            "strictAllowlist": True,
            "allowUnixSockets": [run_tmp_root],
            "allowAllUnixSockets": False,
            "allowLocalBinding": False,
        },
        "filesystem": {
            "denyRead": [str((tmp_path / "private").resolve())],
            "allowRead": [source_root, str(run_root / "assets")],
            "allowWrite": [scratch_root, run_tmp_root],
            "denyWrite": [source_root],
            "allowGitConfig": False,
        },
        "allowPty": False,
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        "allowAppleEvents": False,
    }
    return policy, {
        "source_root": source_root,
        "scratch_root": scratch_root,
        "policy_path": policy_path,
        "run_tmp_root": run_tmp_root,
    }


def _observations(executable: Path, tmp_path: Path) -> list[dict[str, Any]]:
    resolved = str(executable.resolve())
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    control_digest = ask_runtime_control_digest("claude", "claude.ai")
    policy, paths = _policy_fixture(tmp_path)
    policy_hash = _canonical_hash(policy)
    observations: list[dict[str, Any]] = []
    for phase in ("fresh", "resumed"):
        agent_run_id = f"{phase}-agent-run"
        for case, expected in ASK_NATIVE_PROBE_EXPECTATIONS.items():
            record: dict[str, Any] = {
                "phase": phase,
                "case": case,
                "observed": expected,
                "agent_run_id": agent_run_id,
                "session_id": f"{phase}-session",
                "terminal_id": f"{phase}-terminal",
                "process_id": 1001 if phase == "fresh" else 1002,
                "provider_executable": resolved,
                "provider_executable_sha256": executable_sha256,
                "provider_version": "2.1.265",
                "auth_mode": "claude.ai",
                "control_digest": control_digest,
                "policy_hash": policy_hash,
                "policy": policy,
                "srt_runtime_version": "0.0.66",
                "srt_policy_schema_version": ASK_SRT_POLICY_SCHEMA_VERSION,
                **paths,
            }
            if phase == "resumed":
                record["resumed_from_agent_run_id"] = "interrupted-agent-run"
            observations.append(
                {
                    "phase": phase,
                    "case": case,
                    "observed": expected,
                    "receipt": {
                        "source": (
                            "native_runtime"
                            if case in {"native_shell", "native_edit", "unrestricted_read", "web"}
                            else "mcp_response"
                        ),
                        "record": record,
                        "sha256": _canonical_hash(record),
                    },
                }
            )
    return observations


def test_parser_exposes_self_contained_driver_without_external_daemon_controls(
    tmp_path: Path,
) -> None:
    arguments = harness._parser().parse_args(
        [
            "contained-drive",
            "--project-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "evidence"),
        ]
    )

    assert arguments.command == "contained-drive"
    assert arguments.project_root == tmp_path
    assert not hasattr(arguments, "daemon_pid")
    assert not hasattr(arguments, "restart_command_json")


def test_bootstrap_loader_only_bypasses_prior_artifact_in_protected_test_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(tmp_path / "claude")
    marker_path = tmp_path / "bootstrap.json"
    artifact = harness._write_bootstrap_marker(marker_path)
    identity = harness.BootstrapPolicyIdentity(
        runtime_version="0.0.66",
        schema_version=ASK_SRT_POLICY_SCHEMA_VERSION,
        policy_digest="d" * 64,
    )

    monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    with pytest.raises(ValueError, match="loopback test database"):
        harness._load_bootstrap_runtime_validation(
            artifact,
            provider_executable=provider,
            policy_identity=identity,
        )

    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    with pytest.raises(ValueError, match="unique test schema"):
        harness._load_bootstrap_runtime_validation(
            artifact,
            provider_executable=provider,
            policy_identity=identity,
        )

    monkeypatch.setenv("DATABASE_URL", _SCOPED_TEST_DATABASE_URL)
    validation = harness._load_bootstrap_runtime_validation(
        artifact,
        provider_executable=provider,
        policy_identity=identity,
    )

    assert validation.provider == "claude"
    assert validation.provider_executable == str(provider.resolve())
    assert validation.provider_version == "2.1.265"
    assert validation.srt_runtime_version == "0.0.66"
    assert validation.srt_policy_schema_version == ASK_SRT_POLICY_SCHEMA_VERSION
    assert validation.srt_policy_digest == "d" * 64
    with pytest.raises(ValueError, match="schema is unsupported"):
        load_ask_runtime_validation(
            AskRuntimeValidationArtifact(path=marker_path, sha256=artifact.sha256),
            provider_executable=provider,
        )


def test_probe_database_scope_and_worker_environment_are_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    with pytest.raises(ValueError, match="loopback test database"):
        harness._protected_database_url(
            "postgresql://gobby_test:gobby_test@example.com:60892/gobby_test",
            require_unique_schema=False,
        )

    scoped = harness._scoped_database_url(_TEST_DATABASE_URL, _SCHEMA)
    assert parse_qs(urlsplit(scoped).query)["options"] == [f"-csearch_path={_SCHEMA}"]
    assert harness._protected_database_url(scoped, require_unique_schema=True) == _SCHEMA
    with pytest.raises(ValueError, match="unscoped"):
        harness._protected_database_url(scoped, require_unique_schema=False)

    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    monkeypatch.setenv("GOBBY_SESSION_ID", "spoofed")
    monkeypatch.setenv("GOBBY_MACHINE_ID", "spoofed-machine")
    environment = harness._worker_environment(scoped, tmp_path / "gobby", "machine-id")

    assert environment["HOME"] == str(tmp_path / "real-home")
    assert environment["GOBBY_MACHINE_ID"] == "machine-id"
    assert environment["DATABASE_URL"] == scoped
    assert environment["GOBBY_TEST_PROTECT"] == "1"
    assert "ANTHROPIC_API_KEY" not in environment
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in environment
    assert "GOBBY_SESSION_ID" not in environment


def test_wait_for_first_agent_requires_a_running_native_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = [[], ["not-live"], ["not-live", "live"]]

    def run_ids(_database_url: str, _ask_run_id: str) -> list[str]:
        return observed.pop(0)

    monkeypatch.setattr(harness, "_ask_run_ids", run_ids)
    monkeypatch.setattr(
        harness,
        "_agent_is_live",
        lambda _database_url, agent_run_id: agent_run_id == "live",
    )
    monkeypatch.setattr(harness, "_ask_execution_status", lambda *_args: "running")
    monkeypatch.setattr("tests.ask.native_probe_harness.time.sleep", lambda _seconds: None)

    assert (
        harness._wait_for_first_agent(
            _SCOPED_TEST_DATABASE_URL,
            "ask-run",
            deadline_monotonic=time.monotonic() + 1.0,
        )
        == "live"
    )
    assert observed == []


def test_wait_for_first_agent_fails_if_run_finishes_before_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "_ask_run_ids", lambda *_args: [])
    monkeypatch.setattr(harness, "_ask_execution_status", lambda *_args: "completed")

    with pytest.raises(RuntimeError, match="completed before fault injection"):
        harness._wait_for_first_agent(
            _SCOPED_TEST_DATABASE_URL,
            "ask-run",
            deadline_monotonic=time.monotonic() + 1.0,
        )


def _execution_snapshot(
    *,
    run_id: str,
    deadline: str,
    status: str,
    current_agent: str,
    superseded: list[str],
    include_publish: bool,
) -> dict[str, object]:
    steps: list[dict[str, object]] = [
        {
            "id": "step-prepare",
            "step_id": "prepare",
            "status": "completed",
            "output_json": {"snapshot": "stable"},
        },
        {
            "id": "step-investigate",
            "step_id": "investigate",
            "status": "completed" if include_publish else "running",
            "output_json": {"draft": "accepted"} if include_publish else None,
        },
    ]
    if include_publish:
        steps.append(
            {
                "id": "step-publish",
                "step_id": "publish",
                "status": "completed",
                "output_json": {"status": "complete"},
            }
        )
    return {
        "execution": {
            "id": run_id,
            "pipeline_name": "native-ask",
            "status": status,
            "definition_json": {"name": "native-ask", "version": 1},
            "inputs_json": {
                "run_id": run_id,
                "project_id": "project-id",
                "caller_session_id": "caller-id",
                "ask": {
                    "binding": {"deadline_at": deadline},
                    "execution_context": {
                        "run_id": run_id,
                        "project_id": "project-id",
                        "caller_session_id": "caller-id",
                        "project_root": "/source",
                    },
                    "runtime": {
                        "authorities": {
                            "investigate": {
                                "lifecycle": {
                                    "current_agent_run_id": current_agent,
                                    "superseded_agent_run_ids": superseded,
                                }
                            }
                        }
                    },
                },
            },
        },
        "steps": steps,
        "agent_run_ids": [*superseded, current_agent],
    }


def test_recovery_invariants_preserve_deadline_and_completed_checkpoints() -> None:
    before = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="running",
        current_agent="interrupted-agent",
        superseded=[],
        include_publish=False,
    )
    after = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="completed",
        current_agent="successor-agent",
        superseded=["interrupted-agent"],
        include_publish=True,
    )

    harness._assert_recovery_invariants(
        before,
        after,
        interrupted_agent_run_id="interrupted-agent",
    )

    changed_deadline = json.loads(json.dumps(after))
    changed_deadline["execution"]["inputs_json"]["ask"]["binding"]["deadline_at"] = (
        "2026-09-10T12:20:00Z"
    )
    with pytest.raises(RuntimeError, match="deadline"):
        harness._assert_recovery_invariants(
            before,
            changed_deadline,
            interrupted_agent_run_id="interrupted-agent",
        )


def test_recovery_invariants_allow_one_live_child_to_reattach() -> None:
    before = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="running",
        current_agent="reattached-agent",
        superseded=[],
        include_publish=False,
    )
    after = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="completed",
        current_agent="reattached-agent",
        superseded=[],
        include_publish=True,
    )

    harness._assert_recovery_invariants(
        before,
        after,
        interrupted_agent_run_id="reattached-agent",
    )


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _Connection:
    def __init__(
        self,
        snapshots: Mapping[str, dict[str, object]],
        agent_rows: list[dict[str, object]],
    ) -> None:
        self.snapshots = snapshots
        self.agent_rows = agent_rows

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[object, ...]) -> _Cursor:
        run_id = str(parameters[0])
        if "pipeline_executions AS execution" in query:
            execution = self.snapshots[run_id]["execution"]
            assert isinstance(execution, dict)
            return _Cursor([{"record": execution}])
        if "step_executions AS step" in query:
            steps = self.snapshots[run_id]["steps"]
            assert isinstance(steps, list)
            return _Cursor([{"record": step} for step in steps])
        if "agent_runs AS agent" in query:
            return _Cursor(self.agent_rows)
        raise AssertionError(query)


def test_raw_export_hashes_owned_receipt_bytes_and_strips_secret_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    transcript = runtime_root / "transcripts" / "agent.jsonl"
    policy = runtime_root / "sandbox" / "assets" / "settings.json"
    violations = runtime_root / "sandbox" / "logs" / "violations.jsonl"
    transcript.parent.mkdir(parents=True)
    policy.parent.mkdir(parents=True)
    violations.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"mcp_response":"denied"}\n')
    policy.write_bytes(b'{"policy":"exact"}\n')
    violations.write_bytes(b'{"violation":"shell"}\n')
    snapshot = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="completed",
        current_agent="agent-run",
        superseded=[],
        include_publish=True,
    )
    agent_rows = [
        {
            "agent": {
                "id": "agent-run",
                "pid": 123,
                "terminal_id": "terminal-id",
                "resume_metadata_json": {
                    "env": {"ANTHROPIC_API_KEY": "must-not-export", "SAFE": "ok"},
                    "sandbox": {
                        "policy_path": str(policy),
                        "violation_path": str(violations),
                    },
                },
            },
            "session": {"id": "session-id", "transcript_path": str(transcript)},
        }
    ]
    connection = _Connection({"ask-run": snapshot}, agent_rows)
    monkeypatch.setattr(
        "tests.ask.native_probe_harness.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    output_dir = tmp_path / "evidence"

    result = harness._export_raw(
        _SCOPED_TEST_DATABASE_URL,
        ["ask-run"],
        output_dir,
        runtime_root=runtime_root,
        process_sets={"after": {"workers": [], "agents": []}},
    )

    exported = json.loads((output_dir / "raw-probe.json").read_bytes())
    encoded = (output_dir / "raw-probe.json").read_bytes()
    assert result["sha256"] == hashlib.sha256(encoded).hexdigest()
    assert (output_dir / "raw-probe.sha256").read_text(encoding="utf-8").strip() == result["sha256"]
    assert len(exported["receipts"]) == 3
    assert all(Path(receipt["output_path"]).is_file() for receipt in exported["receipts"])
    assert "must-not-export" not in encoded.decode()
    assert exported["agent_runs"][0]["agent"]["resume_metadata_json"]["env"] == {"SAFE": "ok"}


def test_seal_writes_schema_v2_manifest_accepted_by_production_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.ask.test_native_probe_provenance import _raw_probe_fixture

    provider = _provider(tmp_path / "claude")
    observations_path = tmp_path / "observations.json"
    observations = _observations(provider, tmp_path)
    raw_path = _raw_probe_fixture(tmp_path, observations)
    observations_path.write_text(
        json.dumps(observations),
        encoding="utf-8",
    )
    _policy, paths = _policy_fixture(tmp_path)
    monkeypatch.setattr(
        "gobby.ask.runtime_validation.registered_run_tmp",
        lambda _run_root: Path(paths["run_tmp_root"]),
    )
    artifact_path = tmp_path / "validation" / "claude.json"
    manifest_path = tmp_path / "validation" / "manifest.json"
    arguments = argparse.Namespace(
        provider="claude",
        provider_executable=provider,
        auth_mode="claude.ai",
        observations=observations_path,
        output=artifact_path,
        manifest=manifest_path,
        profile=["ask-investigator", "ask-reviewer"],
    )

    assert harness._seal(arguments) == 0

    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["schema_version"] == 2
    artifacts = load_ask_runtime_validation_artifacts(manifest_path)
    assert set(artifacts) == {"ask-investigator", "ask-reviewer"}
    for artifact in artifacts.values():
        validation = load_ask_runtime_validation(
            artifact,
            provider_executable=provider,
        )
        assert validation.verified_artifact is True
    admission = json.loads((manifest_path.parent / "admission.json").read_bytes())
    assert admission["provider"] == "claude"
    assert admission["raw_probe_sha256"] == hashlib.sha256(raw_path.read_bytes()).hexdigest()
    assert admission["profiles"] == ["ask-investigator", "ask-reviewer"]
