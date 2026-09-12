from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.sandbox import compute_sandbox_paths
from gobby.agents.sandbox_policy import gcode_runtime_write_exceptions
from gobby.ask.runtime_validation import (
    ASK_NATIVE_PROBE_EXPECTATIONS,
    AskRuntimeValidationArtifact,
    ask_provider_args,
    ask_runtime_control_digest,
    ask_sandbox_config,
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
        managed_bootstrap_path = f"/probe/{phase}/runtime/grant.json"
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
                "allowRead": [
                    f"/probe/{phase}/runtime/assets",
                    managed_bootstrap_path,
                ],
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
                "managed_bootstrap_path": managed_bootstrap_path,
                "source_root": source_root,
                "scratch_root": scratch_root,
                "srt_runtime_version": "0.0.66",
                "srt_policy_schema_version": 1,
                "raw_evidence": {
                    "raw_probe_sha256": "d" * 64,
                    "receipt_sha256": "e" * 64,
                    "process_start_identity": f"{phase}-start",
                    "response": "synthetic test response",
                    "start_byte": 0,
                    "end_byte": len(b"synthetic test response"),
                },
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


def _runtime_identity() -> dict[str, Any]:
    return {
        "source_head": "a" * 40,
        "gcode": {"path": "/probe/bin/gcode", "sha256": "b" * 64, "version": "1.7.0"},
        "gterm": {"path": "/probe/bin/gterm", "sha256": "c" * 64, "version": None},
    }


def test_probe_artifact_binds_live_provider_version_and_control_digest(tmp_path: Path) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    control_digest = ask_runtime_control_digest("claude", "claude.ai")
    artifact = build_ask_runtime_probe_artifact(
        runtime_identity=_runtime_identity(),
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


@pytest.mark.parametrize("tamper", ["missing", "foreign", "duplicate"])
def test_probe_artifact_requires_exact_managed_grant_binding(
    tmp_path: Path,
    tamper: str,
) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    observations = _observations(executable)
    for observation in observations:
        receipt = observation["receipt"]
        record = receipt["record"]
        grant_path = record["managed_bootstrap_path"]
        if tamper == "missing":
            record.pop("managed_bootstrap_path")
        elif tamper == "foreign":
            record["managed_bootstrap_path"] = str(tmp_path / "foreign" / "grant.json")
        else:
            record["policy"]["filesystem"]["allowRead"].append(grant_path)
            record["policy_hash"] = hashlib.sha256(
                json.dumps(record["policy"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        receipt["sha256"] = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    with pytest.raises(ValueError, match="managed grant"):
        build_ask_runtime_probe_artifact(
            runtime_identity=_runtime_identity(),
            provider="claude",
            provider_executable=executable,
            auth_mode="claude.ai",
            control_digest=ask_runtime_control_digest("claude", "claude.ai"),
            observations=observations,
        )


def test_runtime_loader_rejects_replaced_provider_before_executing_it(tmp_path: Path) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    artifact = build_ask_runtime_probe_artifact(
        runtime_identity=_runtime_identity(),
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
            runtime_identity=_runtime_identity(),
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
            runtime_identity=_runtime_identity(),
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
            runtime_identity=_runtime_identity(),
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
                record["managed_bootstrap_path"] = str(run_root / "grant.json")
                record["run_tmp_root"] = str(short_root)
                record["policy"] = {
                    **record["policy"],
                    "network": {
                        **record["policy"]["network"],
                        "allowUnixSockets": [str(short_root)],
                    },
                    "filesystem": {
                        **record["policy"]["filesystem"],
                        "allowRead": [
                            str(run_root / "assets"),
                            record["managed_bootstrap_path"],
                        ],
                        "allowWrite": [str(run_root / "logs"), str(short_root)],
                    },
                }
                record["policy_hash"] = hashlib.sha256(
                    json.dumps(record["policy"], sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
                receipt["sha256"] = hashlib.sha256(encoded).hexdigest()

        artifact = build_ask_runtime_probe_artifact(
            runtime_identity=_runtime_identity(),
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
            runtime_identity=_runtime_identity(),
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
        runtime_identity=_runtime_identity(),
        provider="claude",
        provider_executable=executable,
        auth_mode="claude.ai",
        control_digest=ask_runtime_control_digest("claude", "claude.ai"),
        observations=observations,
    )

    assert artifact["fresh_probe_passed"] is True
    assert artifact["resume_probe_passed"] is True


@pytest.mark.parametrize("entrypoint", ["build", "load"])
def test_runtime_artifact_requires_raw_provenance_at_every_entrypoint(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    observations = _observations(executable)
    arguments: dict[str, Any] = {
        "provider": "claude",
        "provider_executable": executable,
        "auth_mode": "claude.ai",
        "control_digest": ask_runtime_control_digest("claude", "claude.ai"),
        "observations": observations,
        "runtime_identity": _runtime_identity(),
    }
    if entrypoint == "load":
        artifact = build_ask_runtime_probe_artifact(**arguments)
        for observation in artifact["observations"]:
            receipt = observation["receipt"]
            receipt["record"].pop("raw_evidence", None)
            receipt["sha256"] = hashlib.sha256(
                json.dumps(receipt["record"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        path = tmp_path / "unbound.json"
        digest = write_ask_runtime_probe_artifact(path, artifact)
        with pytest.raises(ValueError, match="raw"):
            load_ask_runtime_validation(
                AskRuntimeValidationArtifact(path=path, sha256=digest),
                provider_executable=executable,
            )
    else:
        for observation in observations:
            receipt = observation["receipt"]
            receipt["record"].pop("raw_evidence", None)
            receipt["sha256"] = hashlib.sha256(
                json.dumps(receipt["record"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        with pytest.raises(ValueError, match="raw"):
            build_ask_runtime_probe_artifact(**arguments)


@pytest.mark.parametrize(
    "tamper",
    [
        "mixed_raw_hash",
        "wrong_span",
        "missing_runtime",
        "invalid_runtime_hash",
        "artifact_raw_hash",
        "process_start_drift",
    ],
)
def test_loader_rejects_inconsistent_sealed_provenance(tmp_path: Path, tamper: str) -> None:
    executable = _provider(tmp_path / "claude", "2.1.265")
    artifact = build_ask_runtime_probe_artifact(
        provider="claude",
        provider_executable=executable,
        auth_mode="claude.ai",
        control_digest=ask_runtime_control_digest("claude", "claude.ai"),
        observations=_observations(executable),
        runtime_identity=_runtime_identity(),
    )
    receipt = artifact["observations"][0]["receipt"]
    if tamper == "mixed_raw_hash":
        receipt["record"]["raw_evidence"]["raw_probe_sha256"] = "f" * 64
    elif tamper == "wrong_span":
        receipt["record"]["raw_evidence"]["end_byte"] += 1
    elif tamper == "missing_runtime":
        del artifact["runtime_identity"]
    elif tamper == "invalid_runtime_hash":
        artifact["runtime_identity"]["gterm"]["sha256"] = "invalid"
    elif tamper == "artifact_raw_hash":
        artifact["raw_probe_sha256"] = "f" * 64
    elif tamper == "process_start_drift":
        receipt["record"]["raw_evidence"]["process_start_identity"] = "other-incarnation"
    receipt["sha256"] = hashlib.sha256(
        json.dumps(receipt["record"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = tmp_path / "changed.json"
    digest = write_ask_runtime_probe_artifact(path, artifact)

    with pytest.raises(ValueError):
        load_ask_runtime_validation(
            AskRuntimeValidationArtifact(path=path, sha256=digest),
            provider_executable=executable,
        )


def _gcode_runtime_home(workspace: Path) -> str:
    return gcode_runtime_write_exceptions(workspace)[0]


def _gcode_runtime_policy(
    *,
    source_root: Path,
    scratch_root: Path,
    run_root: Path,
    gcode_runtime_home: str,
) -> dict[str, Any]:
    """Render the shape of an Ask launch policy that carries a gcode runtime home."""
    return {
        "network": {
            "allowedDomains": [],
            "deniedDomains": [],
            "strictAllowlist": True,
            "allowUnixSockets": [str(run_root / "tmp")],
            "allowAllUnixSockets": False,
            "allowLocalBinding": True,
        },
        "filesystem": {
            "denyRead": [str(source_root)],
            "allowRead": [str(run_root / "assets"), gcode_runtime_home],
            "allowWrite": [str(run_root / "logs"), gcode_runtime_home],
            "denyWrite": [str(source_root), str(scratch_root)],
            "allowGitConfig": False,
        },
        "allowPty": True,
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": True,
        "allowAppleEvents": False,
    }


def test_policy_digest_normalizes_the_workspace_keyed_gcode_runtime_home(tmp_path: Path) -> None:
    """Two launches under different roots agree despite workspace-keyed runtime homes."""
    digests = set()
    for launch in ("first", "second"):
        source_root = tmp_path / launch / "source"
        scratch_root = tmp_path / launch / "scratch"
        run_root = tmp_path / launch / "runtime"
        policy = _gcode_runtime_policy(
            source_root=source_root,
            scratch_root=scratch_root,
            run_root=run_root,
            gcode_runtime_home=_gcode_runtime_home(scratch_root),
        )
        digests.add(
            normalized_ask_srt_policy_digest(
                policy,
                source_root=str(source_root),
                scratch_root=str(scratch_root),
                policy_path=str(run_root / "assets" / "settings.json"),
            )
        )
    assert len(digests) == 1


def test_policy_digest_rejects_another_workspaces_gcode_runtime_home(tmp_path: Path) -> None:
    """Normalizing the runtime home must not blind the digest to a foreign grant."""
    source_root = tmp_path / "source"
    scratch_root = tmp_path / "scratch"
    run_root = tmp_path / "runtime"
    policy_path = str(run_root / "assets" / "settings.json")
    digests = [
        normalized_ask_srt_policy_digest(
            _gcode_runtime_policy(
                source_root=source_root,
                scratch_root=scratch_root,
                run_root=run_root,
                gcode_runtime_home=_gcode_runtime_home(workspace),
            ),
            source_root=str(source_root),
            scratch_root=str(scratch_root),
            policy_path=policy_path,
        )
        for workspace in (scratch_root, tmp_path / "someone-elses-scratch")
    ]
    assert digests[0] != digests[1]


def test_policy_digest_is_stable_across_ask_runs_stages_and_attempts(tmp_path: Path) -> None:
    """Every axis that varies an Ask workspace in production leaves the digest alone."""
    source_root = tmp_path / "source"
    digests = set()
    for ask_run_id in ("ask-run-a", "ask-run-b"):
        for stage in ("investigator", "reviewer"):
            for attempt in (0, 1):
                run_root = tmp_path / ask_run_id / "runtime"
                scratch_root = tmp_path / ask_run_id / "scratch" / f"{stage}-{attempt}"
                policy = _gcode_runtime_policy(
                    source_root=source_root,
                    scratch_root=scratch_root,
                    run_root=run_root,
                    gcode_runtime_home=_gcode_runtime_home(scratch_root),
                )
                digests.add(
                    normalized_ask_srt_policy_digest(
                        policy,
                        source_root=str(source_root),
                        scratch_root=str(scratch_root),
                        policy_path=str(run_root / "assets" / "settings.json"),
                    )
                )
    assert len(digests) == 1


async def test_ask_launch_grants_the_scratch_root_gcode_runtime_home(tmp_path: Path) -> None:
    """The normalizer derives the runtime home from scratch_root; the launch must use it."""
    source_root = tmp_path / "source"
    scratch_root = tmp_path / "scratch"
    source_root.mkdir()
    scratch_root.mkdir()

    paths = await compute_sandbox_paths(
        ask_sandbox_config(str(source_root), str(scratch_root)),
        workspace_path=str(scratch_root),
        provider="claude",
    )

    runtime_home = _gcode_runtime_home(scratch_root)
    assert paths.write_paths.count(runtime_home) == 1
    assert paths.read_paths.count(runtime_home) == 1


@pytest.mark.parametrize("auth_mode", ["claude.ai", "api_key", "api_key_helper"])
def test_provider_args_leave_the_mcp_allowlist_reachable(auth_mode: str) -> None:
    """No Ask launch flag may disable MCP, because Ask runs only on MCP tools.

    `--safe-mode` disables every customization and Claude Code counts MCP
    servers among them, so it strands the allowlist: the agent launches with no
    evidence tools and no way to submit, then dies at the initialization
    timeout without reporting a cause. `--restricted` and `--strict-mcp-config`
    carry the boundary instead.
    """
    arguments = ask_provider_args("claude", auth_mode)

    assert "--safe-mode" not in arguments
    assert "--restricted" in arguments
    assert "--strict-mcp-config" in arguments
    allowlist = arguments[arguments.index("--allowedTools") + 1]
    assert set(allowlist.split(",")) == {
        "mcp__gobby__call_tool",
        "mcp__gobby__get_tool_schema",
        "mcp__gobby__list_tools",
    }
