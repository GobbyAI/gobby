from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gobby.ask.errors import UnsupportedAskRuntime
from gobby.ask.runtime_derivation import (
    ask_runtime_auth_mode,
    assert_ask_srt_policy_boundary,
    derive_ask_runtime_validation,
)
from gobby.ask.runtime_profile import compile_ask_runtime_profile
from gobby.ask.runtime_validation import (
    ASK_RUNTIME_CONTROLS,
    ASK_SRT_POLICY_SCHEMA_VERSION,
    AskRuntimeValidation,
    ask_runtime_control_digest,
)
from gobby.utils.dependency_requirements import SRT_RELEASE

pytestmark = pytest.mark.unit

_PROVIDER = Path("/bin/sh").resolve()
_VERSION = "2.1.0 (Claude Code)"


def _rendered_policy(source_root: str, scratch_root: str, run_root: str) -> dict[str, Any]:
    return {
        "network": {
            "allowedDomains": ["api.anthropic.com", "localhost", "127.0.0.1"],
            "deniedDomains": [],
            "strictAllowlist": True,
            "allowUnixSockets": [f"{run_root}/tmp"],
            "allowAllUnixSockets": False,
            "allowLocalBinding": True,
        },
        "filesystem": {
            "denyRead": [source_root],
            # The scratch root is the agent's workspace: SRT renders it writable and
            # the denyWrite entry is what seals it.
            "allowRead": [scratch_root, f"{run_root}/assets"],
            "allowWrite": [scratch_root, f"{run_root}/logs"],
            "denyWrite": [source_root, scratch_root],
            "allowGitConfig": False,
        },
        "allowPty": True,
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": True,
        "allowAppleEvents": False,
    }


def _derived(monkeypatch: pytest.MonkeyPatch) -> AskRuntimeValidation:
    monkeypatch.setattr(
        "gobby.ask.runtime_validation.probe_native_bin_version", lambda _path: _VERSION
    )
    return derive_ask_runtime_validation(provider="claude", provider_executable=_PROVIDER)


def test_auth_mode_follows_the_credentials_the_provider_will_launch_with() -> None:
    assert ask_runtime_auth_mode("claude", {}) == "claude.ai"
    assert ask_runtime_auth_mode("claude", {"ANTHROPIC_API_KEY": "k"}) == "api_key"
    assert ask_runtime_auth_mode("claude", {"ANTHROPIC_AUTH_TOKEN": "t"}) == "api_key"
    assert ask_runtime_auth_mode("claude", {"ANTHROPIC_API_KEY": ""}) == "claude.ai"
    with pytest.raises(ValueError, match="no proven native Ask controls"):
        ask_runtime_auth_mode("codex", {})


def test_derivation_inspects_the_live_provider_and_pins_no_policy_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation = _derived(monkeypatch)

    assert validation.derived is True
    assert validation.verified_artifact is False
    assert validation.provider_executable == str(_PROVIDER)
    assert (
        validation.provider_executable_sha256 == hashlib.sha256(_PROVIDER.read_bytes()).hexdigest()
    )
    assert validation.provider_version == _VERSION
    assert validation.auth_mode == "claude.ai"
    assert validation.control_digest == ask_runtime_control_digest("claude", "claude.ai")
    assert validation.srt_runtime_version == SRT_RELEASE.version
    assert validation.srt_policy_schema_version == ASK_SRT_POLICY_SCHEMA_VERSION
    assert validation.srt_policy_digest is None
    assert validation.evidence_sha256 is None
    assert validation.controls == ASK_RUNTIME_CONTROLS
    assert validation.fresh_probe_passed is False
    assert validation.resume_probe_passed is False


def test_derivation_reads_the_ambient_environment_when_no_auth_mode_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "live-key")
    validation = _derived(monkeypatch)

    assert validation.auth_mode == "api_key"
    assert validation.control_digest == ask_runtime_control_digest("claude", "api_key")


def test_an_unpinned_policy_digest_is_only_admissible_on_a_derived_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    derived = _derived(monkeypatch)

    with pytest.raises(ValueError, match="SRT policy digest is missing"):
        replace(derived, derived=False, evidence_sha256="a" * 64)
    with pytest.raises(ValueError, match="evidence digest is missing"):
        replace(derived, derived=False, srt_policy_digest="b" * 64)
    with pytest.raises(ValueError, match="derived and attested"):
        replace(derived, verified_artifact=True)


def test_a_derived_profile_binds_its_rendered_policy_semantically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    scratch_root = tmp_path / "scratch"
    run_root = tmp_path / "runtime"
    source_root.mkdir()
    scratch_root.mkdir()
    policy_path = run_root / "assets" / "settings.json"
    policy_path.parent.mkdir(parents=True)
    policy = _rendered_policy(str(source_root), str(scratch_root), str(run_root))
    profile = compile_ask_runtime_profile(
        provider="claude",
        source_root=source_root,
        scratch_root=scratch_root,
        agent_profile_digest="e" * 64,
        model="claude-test",
        reasoning_effort="high",
        endpoint_api_base=None,
        validation=_derived(monkeypatch),
    )
    monkeypatch.setattr(
        "gobby.ask.runtime_profile.probe_native_bin_version", lambda _path: _VERSION
    )
    assert profile.srt_policy_digest is None

    def launch(rendered: dict[str, Any]) -> None:
        body = json.dumps(rendered, sort_keys=True, separators=(",", ":")).encode()
        policy_path.write_bytes(body)
        profile.validate_launch(
            backend="srt",
            enforced=True,
            provider_executable=str(_PROVIDER),
            runtime_version=SRT_RELEASE.version,
            policy_schema_version=ASK_SRT_POLICY_SCHEMA_VERSION,
            policy_hash=hashlib.sha256(body).hexdigest(),
            policy_path=str(policy_path),
            environment={},
        )

    launch(policy)

    widened = _rendered_policy(str(source_root), str(scratch_root), str(run_root))
    widened["network"]["allowedDomains"] = ["api.anthropic.com", "exfil.example"]
    with pytest.raises(UnsupportedAskRuntime, match="widened network egress to exfil.example"):
        launch(widened)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda policy: policy["network"].__setitem__("strictAllowlist", False), "allowlist"),
        (lambda policy: policy["network"].__setitem__("allowAllUnixSockets", True), "allowlist"),
        (lambda policy: policy.__setitem__("enableWeakerNestedSandbox", True), "weakens"),
        (lambda policy: policy.__setitem__("allowAppleEvents", True), "weakens"),
        (lambda policy: policy["filesystem"].__setitem__("allowGitConfig", True), "Git"),
        (lambda policy: policy["filesystem"].__setitem__("denyRead", []), "deny reading"),
        (lambda policy: policy["filesystem"].__setitem__("denyWrite", []), "deny writing"),
    ],
)
def test_the_boundary_assertion_rejects_every_relaxed_policy(
    tmp_path: Path,
    mutate: Any,
    reason: str,
) -> None:
    source_root = str(tmp_path / "source")
    scratch_root = str(tmp_path / "scratch")
    policy = _rendered_policy(source_root, scratch_root, str(tmp_path / "runtime"))
    mutate(policy)

    with pytest.raises(ValueError, match=reason):
        assert_ask_srt_policy_boundary(
            policy,
            provider="claude",
            source_root=source_root,
            scratch_root=scratch_root,
        )


@pytest.mark.parametrize("key", ["allowRead", "allowWrite"])
def test_the_boundary_assertion_rejects_a_grant_inside_the_sealed_source(
    tmp_path: Path,
    key: str,
) -> None:
    source_root = str(tmp_path / "source")
    scratch_root = str(tmp_path / "scratch")
    policy = _rendered_policy(source_root, scratch_root, str(tmp_path / "runtime"))
    policy["filesystem"][key] = [f"{source_root}/src"]

    with pytest.raises(ValueError, match=f"grants {key} inside the sealed source root"):
        assert_ask_srt_policy_boundary(
            policy,
            provider="claude",
            source_root=source_root,
            scratch_root=scratch_root,
        )
