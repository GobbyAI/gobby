from __future__ import annotations

import argparse
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
        if phase == "resumed":
            predecessor = record["resumed_from_agent_run_id"]
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
        raw["agent_runs"].append({"agent": agent, "session": session})
        raw["ask_runs"].append(
            {
                "execution": {"id": f"{phase}-ask", "status": "completed", "project_id": "project"},
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
    raw["process_sets"]["after_cleanup"] = {
        "agents": [{**row["agent"], "live": False} for row in raw["agent_runs"]],
        "workers": [{"pid": 1234, "live": False}],
    }
    raw_path = tmp_path / "raw-probe.json"
    _write_capture(raw_path, raw)
    return raw_path


def test_binds_reviewed_observations_to_captured_files_and_processes(tmp_path: Path) -> None:
    observations = _observations(_provider(tmp_path / "claude"), tmp_path)
    path = _raw_probe_fixture(tmp_path, observations)

    digest, bound = bind_ask_runtime_observations(path, observations)

    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
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
