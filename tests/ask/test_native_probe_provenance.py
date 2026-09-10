from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from gobby.ask.runtime_validation import bind_ask_runtime_observations
from tests.ask import native_probe_harness as harness
from tests.ask.test_native_probe_harness import _observations, _policy_fixture, _provider

pytestmark = pytest.mark.unit


def _write_capture(path: Path, raw: dict[str, Any]) -> None:
    payload = json.dumps(raw, sort_keys=True).encode()
    path.write_bytes(payload)
    path.with_suffix(".sha256").write_text(hashlib.sha256(payload).hexdigest(), encoding="utf-8")


def _raw_probe_fixture(tmp_path: Path, observations: list[dict[str, Any]]) -> Path:
    """Model exported rows/files; all responses here are synthetic unit fixtures."""
    raw: dict[str, Any] = {
        "schema_version": 1,
        "complete": True,
        "capture_errors": [],
        "missing_agent_run_ids": [],
        "runtime_identity": {
            "source_head": "a" * 40,
            "gcode": {"path": "/probe/bin/gcode", "sha256": "b" * 64, "version": "1.7.0"},
            "gterm": {"path": "/probe/bin/gterm", "sha256": "c" * 64, "version": None},
        },
        "ask_runs": [],
        "agent_runs": [],
        "process_sets": {},
        "receipts": [],
        "excluded_receipts": [],
    }
    for phase in ("fresh", "resumed"):
        phase_observations = [row for row in observations if row["phase"] == phase]
        record = phase_observations[0]["receipt"]["record"]
        run_id = record["agent_run_id"]
        agent = {
            "id": run_id,
            "child_session_id": record["session_id"],
            "terminal_id": record["terminal_id"],
            "pid": record["process_id"],
            "provider": "claude",
            "machine_id": "machine",
            "resume_metadata_json": {"project_id": "project"},
        }
        session = {"id": record["session_id"], "machine_id": "machine", "project_id": "project"}
        ids = [run_id]
        lifecycle = {"current_agent_run_id": run_id, "superseded_agent_run_ids": []}
        if phase == "resumed":
            predecessor = record["resumed_from_agent_run_id"]
            lifecycle["superseded_agent_run_ids"] = [predecessor]
            agent["resume_metadata_json"] = {
                "project_id": "project",
                "resumed_from_run_id": predecessor,
            }
            raw["agent_runs"].append(
                {
                    "agent": {
                        **agent,
                        "id": predecessor,
                        "child_session_id": "old-session",
                        "pid": 999,
                    },
                    "session": {**session, "id": "old-session"},
                }
            )
            ids.append(predecessor)
            raw["process_sets"]["interrupted"] = {
                "agents": [
                    {
                        **raw["agent_runs"][-1]["agent"],
                        "live": True,
                        "start_identity": "predecessor-start",
                    }
                ],
                "workers": [],
            }
        raw["agent_runs"].append({"agent": agent, "session": session})
        raw["ask_runs"].append(
            {
                "execution": {
                    "id": f"{phase}-ask",
                    "status": "completed",
                    "project_id": "project",
                    "pipeline_name": "native-ask",
                    "inputs_json": {
                        "ask": {
                            "runtime": {
                                "authorities": {
                                    "investigate": {"lifecycle": lifecycle},
                                }
                            }
                        }
                    },
                },
                "agent_run_ids": ids,
            }
        )
        raw["process_sets"][f"live_{phase}"] = {
            "agents": [
                {
                    **agent,
                    "live": True,
                    "start_identity": f"{phase}-start",
                }
            ],
            "workers": [],
        }
        transcript = bytearray()
        path = tmp_path / f"{phase}-transcript.jsonl"
        for observation in phase_observations:
            start = len(transcript)
            transcript.extend(
                json.dumps(
                    {"tool": observation["case"], "outcome": observation["observed"]}
                ).encode()
                + b"\n"
            )
            observation["receipt"]["evidence"] = {
                "path": str(path),
                "start_byte": start,
                "end_byte": len(transcript),
            }
        path.write_bytes(transcript)
        transcript_hash = hashlib.sha256(transcript).hexdigest()
        for observation in phase_observations:
            observation["receipt"]["evidence"]["sha256"] = transcript_hash
        raw["receipts"].append(
            {
                "agent_run_id": run_id,
                "kind": "provider-transcript-and-mcp-responses",
                "output_path": str(path),
                "sha256": transcript_hash,
                "size_bytes": len(transcript),
            }
        )
        path = tmp_path / f"{phase}-policy.json"
        payload = json.dumps(record["policy"]).encode()
        path.write_bytes(payload)
        raw["receipts"].append(
            {
                "agent_run_id": run_id,
                "kind": "srt-policy",
                "output_path": str(path),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    for snapshot in raw["process_sets"].values():
        for row in snapshot["agents"]:
            row["pgid"] = row["pid"]
            row["terminal_process"] = {"pgid": row["pid"]}
            row["process_group"] = [
                {
                    "pid": row["pid"],
                    "ppid": 1,
                    "pgid": row["pid"],
                    "start_identity": row["start_identity"],
                }
            ]
    for phase in ("fresh", "resumed", "recovery"):
        pid = {"fresh": 700, "resumed": 800, "recovery": 900}[phase]
        raw["process_sets"][f"{phase}_host_launch"] = {
            "agents": [],
            "workers": [],
            "hosts": [
                {
                    "phase": "recover" if phase == "recovery" else phase,
                    "host_pid": pid,
                    "pgid": pid,
                    "worker_pid": 1234,
                    "host_epoch": f"epoch-{pid}",
                    "start_identity": f"host-start-{pid}",
                    "socket_dir": "/probe/host",
                    "control_socket": "/probe/host/control.sock",
                    "frames_socket": "/probe/host/frames.sock",
                    "pidfile": "/probe/host/host.pid",
                    "live": True,
                    "control_socket_exists": True,
                    "frames_socket_exists": True,
                    "spawned_this_construction": True,
                    "adopted": False,
                    "process_group": [
                        {"pid": pid, "pgid": pid, "ppid": 1, "start_identity": f"host-start-{pid}"}
                    ],
                }
            ],
        }
    raw["process_sets"]["worker_launch"] = {
        "agents": [],
        "workers": [{"pid": 1234, "start_identity": "worker-start", "live": True}],
    }
    raw["process_sets"]["after_cleanup"] = {
        "agents": [
            {**row, "live": False, "process_group": []}
            for snapshot in raw["process_sets"].values()
            for row in snapshot["agents"]
        ],
        "workers": [{"pid": 1234, "start_identity": "worker-start", "live": False}],
        "hosts": [
            {
                **row,
                "live": False,
                "process_group": [],
                "control_socket_exists": False,
                "frames_socket_exists": False,
            }
            for snapshot in raw["process_sets"].values()
            for row in snapshot.get("hosts", [])
        ],
    }
    raw_path = tmp_path / "raw-probe.json"
    _write_capture(raw_path, raw)
    return raw_path


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_host_launch",
        "missing_final_host",
        "host_live",
        "host_socket",
        "host_descendant",
        "host_epoch",
        "host_start",
        "host_group",
        "host_worker",
        "host_socket_path",
        "agent_launch_group",
        "agent_final_group",
        "agent_descendant",
    ],
)
def test_rejects_missing_host_or_descendant_cleanup(tmp_path: Path, tamper: str) -> None:
    observations = _observations(_provider(tmp_path / "claude"), tmp_path)
    path = _raw_probe_fixture(tmp_path, observations)
    raw = json.loads(path.read_bytes())
    sets = raw["process_sets"]
    host = sets["fresh_host_launch"]["hosts"][0]
    final_host = sets["after_cleanup"]["hosts"][0]
    if tamper == "missing_host_launch":
        del sets["fresh_host_launch"]
    elif tamper == "missing_final_host":
        sets["after_cleanup"]["hosts"].pop(0)
    elif tamper == "host_live":
        final_host["live"] = True
    elif tamper == "host_socket":
        final_host["frames_socket_exists"] = True
    elif tamper == "host_descendant":
        final_host["process_group"] = host["process_group"]
    elif tamper == "host_epoch":
        final_host["host_epoch"] = "other"
    elif tamper == "host_start":
        final_host["start_identity"] = "other"
    elif tamper == "host_group":
        host["process_group"] = []
    elif tamper == "host_worker":
        host["worker_pid"] = 9876
    elif tamper == "host_socket_path":
        host["control_socket"] = "/foreign/control.sock"
    elif tamper == "agent_launch_group":
        del sets["live_fresh"]["agents"][0]["process_group"]
    elif tamper == "agent_final_group":
        del sets["after_cleanup"]["agents"][0]["process_group"]
    elif tamper == "agent_descendant":
        sets["after_cleanup"]["agents"][0]["process_group"] = [{"pid": 9876}]
    _write_capture(path, raw)
    with pytest.raises(ValueError):
        bind_ask_runtime_observations(path, observations)


def test_binds_reviewed_observations_to_captured_files_and_processes(tmp_path: Path) -> None:
    observations = _observations(_provider(tmp_path / "claude"), tmp_path)
    path = _raw_probe_fixture(tmp_path, observations)

    digest, bound, runtime_identity = bind_ask_runtime_observations(path, observations)

    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert runtime_identity == json.loads(path.read_bytes())["runtime_identity"]
    assert len(bound) == len(observations)
    for original, verified in zip(observations, bound, strict=True):
        evidence = original["receipt"]["evidence"]
        response = (
            Path(evidence["path"])
            .read_bytes()[evidence["start_byte"] : evidence["end_byte"]]
            .decode()
        )
        provenance = verified["receipt"]["record"]["raw_evidence"]
        assert provenance["response"] == response
        assert provenance["raw_probe_sha256"] == digest
        assert provenance["process_start_identity"] == f"{original['phase']}-start"
    assert "raw_evidence" not in observations[0]["receipt"]["record"]


@pytest.mark.parametrize(
    "tamper",
    [
        "raw_hash",
        "receipt_bytes",
        "agent_pid",
        "phase",
        "policy",
        "range",
        "live_child",
        "missing_live",
        "missing_cleanup",
        "predecessor",
        "excluded",
        "record_hash",
    ],
)
def test_rejects_unbacked_or_tampered_observations(tmp_path: Path, tamper: str) -> None:
    observations = _observations(_provider(tmp_path / "claude"), tmp_path)
    path = _raw_probe_fixture(tmp_path, observations)
    raw = json.loads(path.read_bytes())
    first = observations[0]["receipt"]
    if tamper == "raw_hash":
        path.write_bytes(path.read_bytes() + b" ")
    elif tamper == "receipt_bytes":
        Path(first["evidence"]["path"]).write_text("changed", encoding="utf-8")
    else:
        if tamper == "agent_pid":
            raw["agent_runs"][0]["agent"]["pid"] = 42
        elif tamper == "phase":
            raw["ask_runs"][0]["agent_run_ids"] = []
        elif tamper == "policy":
            first["record"]["policy"] = {"changed": True}
            first["sha256"] = hashlib.sha256(
                json.dumps(first["record"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        elif tamper == "range":
            first["evidence"]["end_byte"] = 1000000
        elif tamper == "live_child":
            raw["process_sets"]["after_cleanup"]["agents"][0]["live"] = True
        elif tamper == "missing_live":
            raw["process_sets"]["live_fresh"]["agents"][0]["live"] = False
        elif tamper == "missing_cleanup":
            raw["process_sets"]["after_cleanup"]["agents"] = []
        elif tamper == "predecessor":
            raw["agent_runs"][-1]["agent"]["resume_metadata_json"] = {}
        elif tamper == "excluded":
            raw["excluded_receipts"] = [{"kind": "srt-policy"}]
        elif tamper == "record_hash":
            first["sha256"] = "0" * 64
        _write_capture(path, raw)

    with pytest.raises(ValueError):
        bind_ask_runtime_observations(path, observations)


def test_seal_rejects_observations_without_captured_raw_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path / "claude")
    observations_path = tmp_path / "observations.json"
    observations_path.write_text(json.dumps(_observations(provider, tmp_path)), encoding="utf-8")
    _policy, paths = _policy_fixture(tmp_path)
    monkeypatch.setattr(
        "gobby.ask.runtime_validation.registered_run_tmp",
        lambda _run_root: Path(paths["run_tmp_root"]),
    )
    arguments = argparse.Namespace(
        provider="claude",
        provider_executable=provider,
        auth_mode="claude.ai",
        observations=observations_path,
        output=tmp_path / "validation" / "claude.json",
        manifest=tmp_path / "validation" / "manifest.json",
        profile=["ask-investigator", "ask-reviewer"],
    )

    with pytest.raises(ValueError, match="raw probe"):
        harness._seal(arguments)
    assert not arguments.output.exists()


@pytest.mark.parametrize(
    "tamper",
    [
        "shared_phase_agent",
        "cross_project",
        "wrong_pipeline",
        "foreign_predecessor",
        "predecessor_project",
        "predecessor_machine",
        "pid_drift",
        "terminal_drift",
        "cleanup_start",
        "omitted_worker",
        "omitted_unobserved_agent",
        "missing_runtime",
        "invalid_binary_hash",
        "relative_binary_path",
        "unrelated_lifecycle",
        "missing_predecessor_live",
        "predecessor_pid",
        "incomplete_export",
        "capture_error",
    ],
)
def test_capture_requires_phase_runtime_and_complete_process_identity(
    tmp_path: Path,
    tamper: str,
) -> None:
    observations = _observations(_provider(tmp_path / "claude"), tmp_path)
    path = _raw_probe_fixture(tmp_path, observations)
    raw = json.loads(path.read_bytes())
    snapshots = raw["process_sets"]
    if tamper == "shared_phase_agent":
        raw["ask_runs"][1]["agent_run_ids"].append(raw["ask_runs"][0]["agent_run_ids"][0])
    elif tamper == "cross_project":
        raw["ask_runs"][1]["execution"]["project_id"] = "other-project"
        for row in raw["agent_runs"][1:]:
            row["session"]["project_id"] = "other-project"
            row["agent"]["resume_metadata_json"]["project_id"] = "other-project"
    elif tamper == "wrong_pipeline":
        raw["ask_runs"][0]["execution"]["pipeline_name"] = "unrelated"
    elif tamper == "foreign_predecessor":
        raw["ask_runs"][1]["agent_run_ids"].remove(raw["agent_runs"][1]["agent"]["id"])
    elif tamper in {"predecessor_project", "predecessor_machine"}:
        key = "project_id" if tamper == "predecessor_project" else "machine_id"
        raw["agent_runs"][1]["session"][key] = "other"
    elif tamper in {"pid_drift", "terminal_drift"}:
        row = copy.deepcopy(snapshots["live_fresh"]["agents"][0])
        row["pid" if tamper == "pid_drift" else "terminal_id"] = 99999
        snapshots["drift"] = {"agents": [row], "workers": []}
    elif tamper == "cleanup_start":
        snapshots["after_cleanup"]["agents"][0]["start_identity"] = "other-incarnation"
    elif tamper == "omitted_worker":
        snapshots["live_fresh"]["workers"] = [
            {"pid": 9999, "start_identity": "omitted-worker", "live": True}
        ]
    elif tamper == "omitted_unobserved_agent":
        row = {**snapshots["live_fresh"]["agents"][0], "id": "unobserved-child", "pid": 9999}
        snapshots["live_fresh"]["agents"].append(row)
    elif tamper == "missing_runtime":
        del raw["runtime_identity"]
    elif tamper == "invalid_binary_hash":
        raw["runtime_identity"]["gterm"]["sha256"] = "invalid"
    elif tamper == "relative_binary_path":
        raw["runtime_identity"]["gcode"]["path"] = "relative/gcode"
    elif tamper == "unrelated_lifecycle":
        raw["ask_runs"][1]["execution"]["inputs_json"]["ask"]["runtime"]["authorities"] = {}
    elif tamper == "missing_predecessor_live":
        snapshots["interrupted"]["agents"][0]["live"] = False
    elif tamper == "predecessor_pid":
        raw["agent_runs"][1]["agent"]["pid"] = 42
    elif tamper == "incomplete_export":
        raw["complete"] = False
    elif tamper == "capture_error":
        raw["capture_errors"] = [{"kind": "snapshot-failed"}]
    _write_capture(path, raw)

    with pytest.raises(ValueError):
        bind_ask_runtime_observations(path, observations)
