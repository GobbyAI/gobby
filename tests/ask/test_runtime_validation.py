from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from gobby.ask.runtime_validation import (
    ASK_NATIVE_PROBE_EXPECTATIONS,
    AskRuntimeValidationArtifact,
    ask_runtime_control_digest,
    build_ask_runtime_probe_artifact,
    load_ask_runtime_validation,
    normalized_ask_srt_policy_digest,
    write_ask_runtime_probe_artifact,
)

pytestmark = pytest.mark.unit


def _provider(path: Path, version: str) -> Path:
    path.write_text(f"#!/bin/sh\necho 'Claude Code {version}'\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _observations(executable: Path) -> list[dict[str, Any]]:
    resolved = str(executable.resolve())
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    control_digest = ask_runtime_control_digest("claude", "claude.ai")
    observations: list[dict[str, Any]] = []
    for phase in ("fresh", "resumed"):
        agent_run_id = f"{phase}-agent-run"
        source_root = f"/probe/{phase}/source"
        scratch_root = f"/probe/{phase}/scratch"
        policy_path = f"/probe/{phase}/runtime/assets/settings.json"
        policy = {
            "network": {
                "allowedDomains": [],
                "deniedDomains": [],
                "strictAllowlist": True,
                "allowUnixSockets": [f"/probe/{phase}/runtime/tmp"],
                "allowAllUnixSockets": False,
                "allowLocalBinding": True,
            },
            "filesystem": {
                "denyRead": [source_root],
                "allowRead": [f"/probe/{phase}/runtime/assets"],
                "allowWrite": [f"/probe/{phase}/runtime/logs"],
                "denyWrite": [source_root, scratch_root],
                "allowGitConfig": False,
            },
            "allowPty": True,
            "enableWeakerNestedSandbox": False,
            "enableWeakerNetworkIsolation": True,
            "allowAppleEvents": False,
        }
        policy_hash = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for case, expected in ASK_NATIVE_PROBE_EXPECTATIONS.items():
            record: dict[str, Any] = {
                "phase": phase,
                "case": case,
                "observed": expected,
                "agent_run_id": agent_run_id,
                "session_id": "probe-session",
                "terminal_id": f"{phase}-terminal",
                "process_id": 1001 if phase == "fresh" else 1002,
                "provider_executable": resolved,
                "provider_executable_sha256": executable_sha256,
                "provider_version": "2.1.265",
                "auth_mode": "claude.ai",
                "control_digest": control_digest,
                "policy_hash": policy_hash,
                "policy": json.loads(json.dumps(policy)),
                "policy_path": policy_path,
                "source_root": source_root,
                "scratch_root": scratch_root,
                "srt_runtime_version": "0.0.66",
                "srt_policy_schema_version": 1,
            }
            if phase == "resumed":
                record["resumed_from_agent_run_id"] = "fresh-agent-run"
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
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
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                    },
                }
            )
    return observations


def test_probe_artifact_binds_live_provider_version_and_control_digest(tmp_path: Path) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    control_digest = ask_runtime_control_digest("claude", "claude.ai")
    artifact = build_ask_runtime_probe_artifact(
        provider="claude",
        provider_executable=executable,
        auth_mode="claude.ai",
        control_digest=control_digest,
        observations=_observations(executable),
    )
    artifact_path = tmp_path / "probe.json"
    artifact_sha256 = write_ask_runtime_probe_artifact(artifact_path, artifact)
    reference = AskRuntimeValidationArtifact(
        path=artifact_path,
        sha256=artifact_sha256,
    )

    validation = load_ask_runtime_validation(
        reference,
        provider_executable=executable,
    )
    assert validation.provider_version == "2.1.265"
    assert validation.control_digest == control_digest
    assert validation.fresh_probe_passed is True
    assert validation.resume_probe_passed is True

    _provider(executable, "2.1.266")
    with pytest.raises(ValueError, match="executable|version"):
        load_ask_runtime_validation(reference, provider_executable=executable)

    artifact["control_digest"] = "0" * 64
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact hash"):
        load_ask_runtime_validation(reference, provider_executable=executable)


def test_runtime_loader_rejects_replaced_provider_before_executing_it(tmp_path: Path) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    artifact = build_ask_runtime_probe_artifact(
        provider="claude",
        provider_executable=executable,
        auth_mode="claude.ai",
        control_digest=ask_runtime_control_digest("claude", "claude.ai"),
        observations=_observations(executable),
    )
    artifact_path = tmp_path / "probe.json"
    reference = AskRuntimeValidationArtifact(
        path=artifact_path,
        sha256=write_ask_runtime_probe_artifact(artifact_path, artifact),
    )
    sentinel = tmp_path / "replacement-executed"
    executable.write_text(
        f"#!/bin/sh\ntouch '{sentinel}'\necho 'Claude Code 2.1.265'\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="provider executable changed"):
        load_ask_runtime_validation(reference, provider_executable=executable)

    assert not sentinel.exists()


def test_probe_artifact_requires_every_fresh_and_resumed_boundary(tmp_path: Path) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    observations = _observations(executable)
    observations.pop()
    with pytest.raises(ValueError, match="matrix"):
        build_ask_runtime_probe_artifact(
            provider="claude",
            provider_executable=executable,
            auth_mode="claude.ai",
            control_digest=ask_runtime_control_digest("claude", "claude.ai"),
            observations=observations,
        )


def test_probe_artifact_rejects_self_attested_receipt_strings(tmp_path: Path) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    observations = _observations(executable)
    observations[0]["receipt"] = "the agent claimed this was denied"

    with pytest.raises(ValueError, match="structured raw receipt"):
        build_ask_runtime_probe_artifact(
            provider="claude",
            provider_executable=executable,
            auth_mode="claude.ai",
            control_digest=ask_runtime_control_digest("claude", "claude.ai"),
            observations=observations,
        )


def test_probe_artifact_rejects_policy_widening_between_fresh_and_resume(
    tmp_path: Path,
) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    observations = _observations(executable)
    for observation in observations:
        if observation["phase"] != "resumed":
            continue
        receipt = observation["receipt"]
        record = receipt["record"]
        record["policy"]["network"]["allowedDomains"] = ["example.com"]
        record["policy_hash"] = hashlib.sha256(
            json.dumps(record["policy"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        receipt["sha256"] = hashlib.sha256(encoded).hexdigest()

    with pytest.raises(ValueError, match="policy"):
        build_ask_runtime_probe_artifact(
            provider="claude",
            provider_executable=executable,
            auth_mode="claude.ai",
            control_digest=ask_runtime_control_digest("claude", "claude.ai"),
            observations=observations,
        )


def test_probe_artifact_normalizes_only_registered_macos_run_temp_roots(
    tmp_path: Path,
) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    observations = _observations(executable)
    short_roots = {
        phase: Path(tempfile.mkdtemp(prefix="gobby-")).resolve() for phase in ("fresh", "resumed")
    }
    try:
        for phase, short_root in short_roots.items():
            run_root = tmp_path / phase / "runtime"
            (run_root / "assets").mkdir(parents=True)
            (run_root / "tmp-path").write_text(str(short_root), encoding="utf-8")
            for observation in observations:
                if observation["phase"] != phase:
                    continue
                receipt = observation["receipt"]
                record = receipt["record"]
                record["policy_path"] = str(run_root / "assets" / "settings.json")
                record["run_tmp_root"] = str(short_root)
                record["policy"] = {
                    **record["policy"],
                    "network": {
                        **record["policy"]["network"],
                        "allowUnixSockets": [str(short_root)],
                    },
                    "filesystem": {
                        **record["policy"]["filesystem"],
                        "allowRead": [str(run_root / "assets")],
                        "allowWrite": [str(run_root / "logs"), str(short_root)],
                    },
                }
                record["policy_hash"] = hashlib.sha256(
                    json.dumps(record["policy"], sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
                receipt["sha256"] = hashlib.sha256(encoded).hexdigest()

        artifact = build_ask_runtime_probe_artifact(
            provider="claude",
            provider_executable=executable,
            auth_mode="claude.ai",
            control_digest=ask_runtime_control_digest("claude", "claude.ai"),
            observations=observations,
        )
        fresh_record = observations[0]["receipt"]["record"]
        with pytest.raises(ValueError, match="registration"):
            normalized_ask_srt_policy_digest(
                fresh_record["policy"],
                source_root=fresh_record["source_root"],
                scratch_root=fresh_record["scratch_root"],
                policy_path=fresh_record["policy_path"],
                run_tmp_root=str(short_roots["resumed"]),
                require_registered_run_tmp=True,
            )
    finally:
        for short_root in short_roots.values():
            shutil.rmtree(short_root)

    assert artifact["srt_policy_digest"]


def test_runtime_manifest_loads_only_pinned_contained_artifacts(tmp_path: Path) -> None:
    from gobby.ask.runtime_validation import load_ask_runtime_validation_artifacts

    manifest = tmp_path / "manifest.json"
    with pytest.raises(FileNotFoundError):
        load_ask_runtime_validation_artifacts(manifest)

    outside = tmp_path.parent / "outside-ask-validation.json"
    outside.write_text("{}\n", encoding="utf-8")
    outside_sha256 = hashlib.sha256(outside.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profiles": {
                    "ask-investigator": {
                        "path": "../outside-ask-validation.json",
                        "sha256": outside_sha256,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="escapes"):
        load_ask_runtime_validation_artifacts(manifest)

    artifact = tmp_path / "claude.json"
    artifact.write_text("{}\n", encoding="utf-8")
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profiles": {
                    "ask-investigator": {
                        "path": artifact.name,
                        "sha256": artifact_sha256,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_ask_runtime_validation_artifacts(manifest)
    assert loaded == {
        "ask-investigator": AskRuntimeValidationArtifact(
            path=artifact.resolve(),
            sha256=artifact_sha256,
        )
    }


def test_probe_phase_cannot_mix_runtime_policy_identities(tmp_path: Path) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    observations = _observations(executable)
    receipt = observations[0]["receipt"]
    record = receipt["record"]
    record["policy"]["network"]["allowedDomains"] = ["example.com"]
    record["policy_hash"] = hashlib.sha256(
        json.dumps(record["policy"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    receipt["sha256"] = hashlib.sha256(encoded).hexdigest()

    with pytest.raises(ValueError, match="one process identity"):
        build_ask_runtime_probe_artifact(
            provider="claude",
            provider_executable=executable,
            auth_mode="claude.ai",
            control_digest=ask_runtime_control_digest("claude", "claude.ai"),
            observations=observations,
        )


def test_fresh_and_resumed_probes_may_use_separate_managed_runs(tmp_path: Path) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    observations = _observations(executable)
    for observation in observations:
        if observation["phase"] != "resumed":
            continue
        receipt = observation["receipt"]
        record = receipt["record"]
        record["resumed_from_agent_run_id"] = "interrupted-agent-run"
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        receipt["sha256"] = hashlib.sha256(encoded).hexdigest()

    artifact = build_ask_runtime_probe_artifact(
        provider="claude",
        provider_executable=executable,
        auth_mode="claude.ai",
        control_digest=ask_runtime_control_digest("claude", "claude.ai"),
        observations=observations,
    )

    assert artifact["fresh_probe_passed"] is True
    assert artifact["resume_probe_passed"] is True
