"""Render a resolved sandbox policy as sandbox-runtime settings."""

from __future__ import annotations

import os
from typing import Any

from gobby.agents.sandbox import ResolvedSandboxPaths
from gobby.agents.sandbox_policy import policy_subtree_prefix

# sandbox-runtime's getDefaultWritePaths() (dist/sandbox/sandbox-utils.js) joins
# every policy's allowWrite, so a deny beneath one still decides a write.
_SRT_DEFAULT_WRITE_PATHS = (
    "/dev/stdout",
    "/dev/stderr",
    "/dev/null",
    "/dev/tty",
    "/dev/dtracehelper",
    "/dev/autofs_nowait",
    "/tmp/claude",
    "/private/tmp/claude",
    "~/.npm/_logs",
    "~/.claude/debug",
)


def _effective_deny_write(allow_write: list[str], deny_write: list[str]) -> list[str]:
    """Keep the write denies that share a subtree with a write grant.

    Writes outside every grant are already denied by default. Seatbelt's compile
    cost grows faster than the deny count: 1978 entries, most of them under
    unwritable ancestors, cost 13.7 s of CPU per launch.
    """
    defaults = [os.path.expanduser(path) for path in _SRT_DEFAULT_WRITE_PATHS]
    # SRT grants each default by its realpath (normalizePathForSandbox); allow_write
    # is already canonical.
    grants = [
        policy_subtree_prefix(path)
        for path in {*defaults, *map(os.path.realpath, defaults), *allow_write}
    ]
    kept = []
    for deny in deny_write:
        prefix = policy_subtree_prefix(deny)
        if any(prefix.startswith(grant) or grant.startswith(prefix) for grant in grants):
            kept.append(deny)
    return kept


def render_srt_settings(paths: ResolvedSandboxPaths) -> dict[str, Any]:
    """Render canonical resolved policy to SRT's validated settings schema."""
    credentials = [
        {
            "name": credential.name,
            "mode": credential.mode,
            "injectHosts": credential.inject_hosts,
        }
        for credential in paths.credential_env_vars
    ]
    network: dict[str, Any] = {
        "allowedDomains": paths.allowed_domains,
        "deniedDomains": paths.denied_domains,
        "strictAllowlist": True,
        "allowUnixSockets": paths.allow_unix_sockets,
        "allowAllUnixSockets": False,
        # Loopback egress: ghook and `gobby mcp-server` POST to the daemon
        # HTTP port and gcode connects directly to the hub Postgres. SRT only
        # exposes a boolean (it renders `remote ip "localhost:*"`); the
        # external-domain allowlist stays enforced for non-loopback egress.
        "allowLocalBinding": True,
    }
    if credentials:
        network["tlsTerminate"] = {}
    settings: dict[str, Any] = {
        "network": network,
        "filesystem": {
            "denyRead": paths.deny_read_paths,
            "allowRead": paths.read_paths,
            "allowWrite": paths.write_paths,
            "denyWrite": _effective_deny_write(paths.write_paths, paths.deny_write_paths),
            "allowGitConfig": False,
        },
        "allowPty": True,
        "enableWeakerNestedSandbox": False,
        # Only adds the com.apple.trustd.agent mach-lookup: native TLS
        # verification (codex's hosted-apps MCP client, curl, git, Go tools)
        # fails its handshake without it. Egress stays proxy-filtered.
        "enableWeakerNetworkIsolation": True,
        "allowAppleEvents": False,
        # Harmless denials that flooded violation logs (11.77 GB by 2026-10-01):
        # libsystem reads kern.iossupportversion at every exec, compilers query
        # free disk space, codex probes configd reachability. Enforcement is
        # unchanged; SRT only stops recording lines containing these substrings.
        # Also: getaddrinfo's AI_ADDRCONFIG getifaddrs probes (net.link.addr),
        # file watchers' FSEvents and DiskArbitration lookups, and CPU feature
        # sysctls. Never add network-outbound: the failed-DNS fallback that
        # follows net.link.addr has no target, and egress denials must stay visible.
        "ignoreViolations": {
            "*": [
                "sysctl-read kern.iossupportversion",
                "system-info vfs.disk-space",
                "mach-lookup com.apple.SystemConfiguration.configd",
                "system-info net.link.addr",
                "mach-lookup com.apple.FSEvents",
                "mach-lookup com.apple.DiskArbitration.diskarbitrationd",
                "sysctl-read hw.optional.",
                "sysctl-read hw.cpusubfamily",
            ]
        },
    }
    if paths.provider == "grok":
        # Grok queries SkyLight flags on its UI thread, including after compact.
        settings["network"]["allowMachLookup"] = ["com.apple.windowserver.active"]
    if credentials:
        settings["credentials"] = {"envVars": credentials, "allowPlaintextInject": False}
    return settings
