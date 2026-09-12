"""Derive Ask runtime validation by live inspection instead of a frozen probe.

The probe artifact in :mod:`gobby.ask.runtime_validation` is an attestation: a
recording of one supervised run whose fourteen boundary cases behaved as the
contract requires. It is the strongest evidence Ask can carry, and nothing in
the product produces it — so making Ask exist only when one is present takes the
whole feature offline.

Derivation is the ordinary path. It inspects the live provider binary and the
installed SRT runtime, recomputes the control digest from the effective profile,
and leaves the SRT policy digest unpinned. The boundary itself is unchanged: the
provider arguments, the sandbox config, and the MCP allowlist are all compiled
from the same functions either way, and a derived launch binds its rendered SRT
policy semantically instead of comparing it to an earlier recording.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gobby.agents.sandbox_policy import allowed_domains as sandbox_allowed_domains
from gobby.ask.runtime_validation import (
    ASK_RUNTIME_CONTROLS,
    ASK_SRT_POLICY_SCHEMA_VERSION,
    AskRuntimeValidation,
    _provider_identity,
    _validate_srt_policy_schema,
    ask_runtime_control_digest,
    ask_sandbox_config,
)
from gobby.utils.dependency_requirements import SRT_RELEASE

_API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_LOOPBACK_DOMAINS = frozenset({"localhost", "127.0.0.1"})


def ask_runtime_auth_mode(
    provider: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve the auth mode the provider will actually launch with."""
    if provider != "claude":
        raise ValueError(f"provider {provider!r} has no proven native Ask controls")
    variables = os.environ if environment is None else environment
    if any(variables.get(name) for name in _API_KEY_ENV_VARS):
        return "api_key"
    return "claude.ai"


def derive_ask_runtime_validation(
    *,
    provider: str,
    provider_executable: Path,
    auth_mode: str | None = None,
) -> AskRuntimeValidation:
    """Inspect the live provider and SRT runtime to build an unpinned validation."""
    mode = ask_runtime_auth_mode(provider) if auth_mode is None else auth_mode
    executable, executable_sha256, version = _provider_identity(provider_executable)
    return AskRuntimeValidation(
        provider=provider,
        provider_executable=executable,
        provider_executable_sha256=executable_sha256,
        provider_version=version,
        auth_mode=mode,
        control_digest=ask_runtime_control_digest(provider, mode),
        srt_runtime_version=SRT_RELEASE.version,
        srt_policy_schema_version=ASK_SRT_POLICY_SCHEMA_VERSION,
        srt_policy_digest=None,
        controls=ASK_RUNTIME_CONTROLS,
        fresh_probe_passed=False,
        resume_probe_passed=False,
        evidence_sha256=None,
        derived=True,
    )


def assert_ask_srt_policy_boundary(
    policy: Mapping[str, Any],
    *,
    provider: str,
    source_root: str,
    scratch_root: str,
) -> None:
    """Assert a rendered SRT policy still expresses the Ask isolation boundary.

    An attested launch compares the rendered policy to the digest its probe
    observed. A derived launch has no earlier digest, so it asserts the four
    properties ``ask_sandbox_config`` exists to produce: egress confined to the
    provider API and loopback, the sealed source unreadable and unwritable, the
    scratch root unwritable, and no weakened isolation switch.
    """
    _validate_srt_policy_schema(policy)
    network = policy["network"]
    filesystem = policy["filesystem"]
    permitted = (
        set(sandbox_allowed_domains(ask_sandbox_config(source_root, scratch_root), provider, None))
        | _LOOPBACK_DOMAINS
    )
    widened = sorted(set(network["allowedDomains"]) - permitted)
    if widened:
        raise ValueError(f"Ask SRT policy widened network egress to {', '.join(widened)}")
    if not network["strictAllowlist"] or network["allowAllUnixSockets"]:
        raise ValueError("Ask SRT policy does not confine network egress to its allowlist")
    for name in ("enableWeakerNestedSandbox", "allowAppleEvents"):
        if policy[name]:
            raise ValueError(f"Ask SRT policy weakens isolation through {name}")
    if filesystem["allowGitConfig"]:
        raise ValueError("Ask SRT policy exposes the ambient Git configuration")
    source = Path(source_root).expanduser().resolve(strict=False)
    scratch = Path(scratch_root).expanduser().resolve(strict=False)
    if source not in _resolved(filesystem["denyRead"]):
        raise ValueError("Ask SRT policy does not deny reading the sealed source root")
    denied_write = _resolved(filesystem["denyWrite"])
    for root, label in ((source, "source"), (scratch, "scratch")):
        if root not in denied_write:
            raise ValueError(f"Ask SRT policy does not deny writing the {label} root")
    # The scratch root is the agent's workspace and is always in allowWrite; the
    # denyWrite entry asserted above is what actually seals it, and SRT gives deny
    # precedence. The sealed source appears in neither list.
    for key in ("allowRead", "allowWrite"):
        for granted in _resolved(filesystem[key]):
            if granted == source or granted.is_relative_to(source):
                raise ValueError(f"Ask SRT policy grants {key} inside the sealed source root")


def _resolved(values: list[str]) -> set[Path]:
    return {Path(value).expanduser().resolve(strict=False) for value in values}


__all__ = [
    "ask_runtime_auth_mode",
    "assert_ask_srt_policy_boundary",
    "derive_ask_runtime_validation",
]
