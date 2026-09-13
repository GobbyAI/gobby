"""Provider arguments and normalized sandbox policy for managed Ask stages."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_policy import (
    SRT_SETTINGS_RELATIVE_PATH,
    gcode_runtime_write_exceptions,
    registered_run_tmp,
)
from gobby.ask.claims import canonical_json

ASK_RUNTIME_CONTROLS = frozenset(
    {
        "mcp_allowlist_exact",
        "native_execution_denied",
        "native_mutation_denied",
        "network_denied",
        "resume_preserves_boundary",
        "source_outside_writable_root",
        "subagents_denied",
    }
)

_SUPPORTED_CLAUDE_AUTH_MODES = frozenset({"claude.ai", "api_key", "api_key_helper"})


def ask_provider_args(provider: str, auth_mode: str) -> tuple[str, ...]:
    if provider != "claude":
        raise ValueError(f"provider {provider!r} has no proven native Ask controls")
    if auth_mode not in _SUPPORTED_CLAUDE_AUTH_MODES:
        raise ValueError("Ask runtime auth mode is unsupported")
    # `--safe-mode` is deliberately absent. It disables every customization, and
    # Claude Code counts MCP servers among them, so a safe-mode launch drops the
    # `--mcp-config` surface the allowlist below names: the agent starts with no
    # evidence tools and no way to submit. `--restricted` already removes the
    # command- and code-running built-ins and WebFetch, ignores the user, project
    # and local settings files, and confines the file tools to the working
    # directories; `--strict-mcp-config` narrows MCP to the supplied config alone.
    # Together those are the boundary, and they keep the Ask tools reachable.
    arguments = (
        # Interactive startup can build the first prompt before MCP connects.
        # Ask stages are unattended: print mode waits for the configured tools
        # and exits after the agent submits its result and finishes its turn.
        "--print",
        "--restricted",
        "--disable-slash-commands",
        "--no-chrome",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--tools",
        "",
        "--allowedTools",
        "mcp__gobby__call_tool,mcp__gobby__get_tool_schema,mcp__gobby__list_tools",
        "--strict-mcp-config",
    )
    return ("--bare", *arguments) if auth_mode in {"api_key", "api_key_helper"} else arguments


def ask_sandbox_config(source_root: str, scratch_root: str) -> SandboxConfig:
    return SandboxConfig(
        enabled=True,
        backend="srt",
        mode="restrictive",
        allow_network=False,
        extra_deny_read_paths=[source_root],
        extra_deny_write_paths=[source_root, scratch_root],
        allow_git_network=False,
        allow_package_registries=False,
    )


def ask_runtime_control_digest(provider: str, auth_mode: str) -> str:
    material = {
        "provider": provider,
        "provider_args": list(ask_provider_args(provider, auth_mode)),
        "builtin_tools": ["EndConversation"],
        "auto_approve": False,
        "sandbox": ask_sandbox_config("<source>", "<scratch>").model_dump(mode="json"),
        "controls": sorted(ASK_RUNTIME_CONTROLS),
    }
    return hashlib.sha256(canonical_json(material)).hexdigest()


def normalized_ask_srt_policy_digest(
    policy: Mapping[str, Any],
    *,
    source_root: str,
    scratch_root: str,
    policy_path: str,
    run_tmp_root: str | None = None,
    require_registered_run_tmp: bool = False,
    managed_bootstrap_path: str | None = None,
) -> str:
    """Hash rendered SRT semantics across equivalent managed launch roots."""
    validate_srt_policy_schema(policy)
    policy_file = Path(policy_path).expanduser().resolve(strict=False)
    if tuple(policy_file.parts[-len(SRT_SETTINGS_RELATIVE_PATH.parts) :]) != tuple(
        SRT_SETTINGS_RELATIVE_PATH.parts
    ):
        raise ValueError("Ask runtime probe policy path is not a managed SRT settings path")
    run_root = policy_file.parents[len(SRT_SETTINGS_RELATIVE_PATH.parts) - 1]
    scratch = Path(scratch_root).expanduser().resolve(strict=False)
    roots: list[tuple[Path, str]] = [
        (Path(source_root).expanduser().resolve(strict=False), "<source>"),
        (scratch, "<scratch>"),
        (run_root, "<run>"),
        # gcode's generated runtime home is keyed by SHA-256 of the sandbox workspace and
        # lives under GOBBY_HOME, outside every launch root. Ask gives each stage and each
        # repair attempt its own workspace, so hashing that path verbatim would make the
        # digest differ on every launch. Relabelling it keeps the binding honest: a policy
        # granting some other workspace's runtime home still fails to match.
        (Path(gcode_runtime_write_exceptions(scratch)[0]), "<gcode-runtime>"),
    ]
    registered_tmp = registered_run_tmp(run_root)
    if run_tmp_root is not None:
        supplied_tmp = Path(run_tmp_root).expanduser().resolve(strict=False)
        system_tmp = Path(tempfile.gettempdir()).resolve()
        if (
            supplied_tmp.parent != system_tmp
            or not supplied_tmp.name.startswith("gobby-")
            or len(supplied_tmp.name) != 14
        ):
            raise ValueError("Ask runtime run temp root is not a managed short temp")
        if registered_tmp is not None and supplied_tmp != registered_tmp:
            raise ValueError("Ask runtime run temp root does not match its registration")
        if require_registered_run_tmp and registered_tmp is None:
            raise ValueError("Ask runtime run temp root is not registered to this launch")
        roots.append((supplied_tmp, "<run-tmp>"))
    elif registered_tmp is not None:
        roots.append((registered_tmp, "<run-tmp>"))

    def normalize_path(value: object) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("Ask runtime SRT policy path is invalid")
        resolved = Path(value).expanduser().resolve(strict=False)
        for root, label in roots:
            if resolved == root or resolved.is_relative_to(root):
                relative = resolved.relative_to(root)
                suffix = "" if relative == Path(".") else f"/{relative.as_posix()}"
                return f"{label}{suffix}"
        return str(resolved)

    normalized = json.loads(canonical_json(policy))
    filesystem = normalized["filesystem"]
    for key in ("denyRead", "allowRead", "allowWrite", "denyWrite"):
        filesystem[key] = [normalize_path(value) for value in filesystem[key]]
    if managed_bootstrap_path is not None:
        bootstrap = Path(managed_bootstrap_path).expanduser().resolve(strict=False)
        if bootstrap.name != "grant.json" or bootstrap.parent != run_root:
            raise ValueError("Ask runtime managed grant is outside its launch root")
        normalized_bootstrap = normalize_path(str(bootstrap))
        if filesystem["allowRead"].count(normalized_bootstrap) != 1:
            raise ValueError("Ask runtime managed grant is not uniquely readable")
        filesystem["allowRead"].remove(normalized_bootstrap)
    network = normalized["network"]
    network["allowUnixSockets"] = [normalize_path(value) for value in network["allowUnixSockets"]]
    return hashlib.sha256(canonical_json(normalized)).hexdigest()


def validate_srt_policy_schema(policy: Mapping[str, Any]) -> None:
    required_top = {
        "network",
        "filesystem",
        "allowPty",
        "enableWeakerNestedSandbox",
        "enableWeakerNetworkIsolation",
        "allowAppleEvents",
    }
    policy_keys = set(policy)
    if policy_keys != required_top and policy_keys != required_top | {"credentials"}:
        raise ValueError("Ask runtime rendered SRT policy schema changed")
    network = policy.get("network")
    filesystem = policy.get("filesystem")
    if not isinstance(network, Mapping) or set(network) != {
        "allowedDomains",
        "deniedDomains",
        "strictAllowlist",
        "allowUnixSockets",
        "allowAllUnixSockets",
        "allowLocalBinding",
    }:
        raise ValueError("Ask runtime rendered SRT network policy schema changed")
    if not isinstance(filesystem, Mapping) or set(filesystem) != {
        "denyRead",
        "allowRead",
        "allowWrite",
        "denyWrite",
        "allowGitConfig",
    }:
        raise ValueError("Ask runtime rendered SRT filesystem policy schema changed")
    for owner, keys in (
        (network, ("allowedDomains", "deniedDomains", "allowUnixSockets")),
        (filesystem, ("denyRead", "allowRead", "allowWrite", "denyWrite")),
    ):
        for key in keys:
            value = owner.get(key)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError("Ask runtime rendered SRT policy list is invalid")
    boolean_values = (
        network.get("strictAllowlist"),
        network.get("allowAllUnixSockets"),
        network.get("allowLocalBinding"),
        filesystem.get("allowGitConfig"),
        *(policy.get(key) for key in required_top - {"network", "filesystem"}),
    )
    if not all(isinstance(value, bool) for value in boolean_values):
        raise ValueError("Ask runtime rendered SRT policy boolean is invalid")
