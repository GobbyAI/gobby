"""Pinned Sandbox Runtime launch preparation for managed CLI agents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.agents.sandbox_policy import secure_policy_directory
from gobby.paths import get_gobby_home

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from gobby.agents.sandbox import SandboxConfig, SandboxResolver

SRT_PACKAGE = "@anthropic-ai/sandbox-runtime"
SRT_VERSION = "0.0.66"
SRT_TARBALL_URL = (
    "https://registry.npmjs.org/@anthropic-ai/sandbox-runtime/-/sandbox-runtime-0.0.66.tgz"
)
SRT_TARBALL_SHA256 = "10088a88db2d734d3a7ccf57d83e0b781ab08669361b45947637e3fd51d7c4ee"
SRT_NPM_INTEGRITY = (
    "sha512-OE7QiGZJXe7ZshP47U2vk2z9FGSyiSN4ca9krVrE28LS2Qj0AHRWZz+"
    "gAce6FzG3gx/4OjNFwIhDuHXnI0WWwA=="
)
SRT_LOCKFILE_SHA256 = "aa0e24fece2864c9a561db55ac5d528af202b17107675be89c1bce65c289ee3f"


class SrtRuntimeError(RuntimeError):
    """Raised when the pinned runtime or policy cannot be used safely."""


@dataclass(frozen=True)
class SrtInstallation:
    root: Path
    node: Path
    runner: Path
    package_json: Path


@dataclass(frozen=True)
class SandboxLaunch:
    backend: str
    enforced: bool
    provider_args: list[str] = field(default_factory=list)
    provider_env: dict[str, str] = field(default_factory=dict)
    runtime_version: str | None = None
    policy_hash: str | None = None
    policy_path: str | None = None
    violation_path: str | None = None
    node_path: str | None = None
    runner_path: str | None = None

    def wrap(self, command: Sequence[str]) -> list[str]:
        """Wrap one provider argv exactly once when SRT is active."""
        if self.backend != "srt" or not self.enforced:
            return list(command)
        if not all((self.node_path, self.runner_path, self.policy_path, self.violation_path)):
            raise SrtRuntimeError("SRT launch metadata is incomplete")
        assert self.node_path is not None
        assert self.runner_path is not None
        assert self.policy_path is not None
        assert self.violation_path is not None
        return [
            self.node_path,
            self.runner_path,
            "--settings",
            self.policy_path,
            "--violations",
            self.violation_path,
            "--",
            *command,
        ]

    def metadata(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "enforced": self.enforced,
            "runtime_version": self.runtime_version,
            "policy_hash": self.policy_hash,
            "policy_path": self.policy_path,
            "violation_path": self.violation_path,
        }


def srt_install_root() -> Path:
    return get_gobby_home() / "tools" / "srt" / SRT_VERSION


def verify_srt_installation() -> SrtInstallation:
    """Verify the pinned package, receipt, runner, and absolute Node runtime."""
    root = srt_install_root().resolve(strict=False)
    receipt_path = root / "receipt.json"
    runner = root / "runner.mjs"
    bundled_runner = Path(__file__).with_name("srt_runner.mjs")
    package_json = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "package.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        package = json.loads(package_json.read_text(encoding="utf-8"))
        runner_bytes = runner.read_bytes()
        bundled_runner_bytes = bundled_runner.read_bytes()
        lockfile_bytes = (root / "package-lock.json").read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise SrtRuntimeError(
            f"managed SRT {SRT_VERSION} is missing or invalid; rerun `gobby install`"
        ) from exc

    expected_receipt = {
        "package": SRT_PACKAGE,
        "version": SRT_VERSION,
        "tarball_url": SRT_TARBALL_URL,
        "tarball_sha256": SRT_TARBALL_SHA256,
        "npm_integrity": SRT_NPM_INTEGRITY,
        "lockfile_sha256": SRT_LOCKFILE_SHA256,
    }
    if any(receipt.get(key) != value for key, value in expected_receipt.items()):
        raise SrtRuntimeError("managed SRT receipt does not match Gobby's pinned runtime")
    if package.get("name") != SRT_PACKAGE or package.get("version") != SRT_VERSION:
        raise SrtRuntimeError("managed SRT package identity does not match its receipt")
    runner_sha256 = hashlib.sha256(runner_bytes).hexdigest()
    if receipt.get("runner_sha256") != runner_sha256:
        raise SrtRuntimeError("managed SRT runner checksum mismatch")
    if runner_sha256 != hashlib.sha256(bundled_runner_bytes).hexdigest():
        raise SrtRuntimeError("managed SRT runner does not match this Gobby installation")
    if hashlib.sha256(lockfile_bytes).hexdigest() != SRT_LOCKFILE_SHA256:
        raise SrtRuntimeError("managed SRT lockfile checksum mismatch")

    node_raw = shutil.which("node")
    if not node_raw:
        raise SrtRuntimeError("Node.js 20.11 or newer is required for managed SRT")
    node = Path(node_raw).resolve(strict=True)
    return SrtInstallation(root=root, node=node, runner=runner.resolve(), package_json=package_json)


def render_srt_settings(paths: Any) -> dict[str, Any]:
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
            "denyWrite": paths.deny_write_paths,
            "allowGitConfig": False,
        },
        "allowPty": True,
        "enableWeakerNestedSandbox": False,
        # Only adds the com.apple.trustd.agent mach-lookup: native TLS
        # verification (codex's hosted-apps MCP client, curl, git, Go tools)
        # fails its handshake without it. Egress stays proxy-filtered.
        "enableWeakerNetworkIsolation": True,
        "allowAppleEvents": False,
    }
    if credentials:
        settings["credentials"] = {"envVars": credentials, "allowPlaintextInject": False}
    return settings


async def prepare_sandbox_launch(
    *,
    config: SandboxConfig,
    provider: str,
    workspace_path: str,
    run_id: str,
    resolver: SandboxResolver | None,
    daemon_port: int,
    websocket_port: int,
    api_base: str | None,
    env: Mapping[str, str],
) -> SandboxLaunch:
    """Resolve and preflight the explicit backend without any fallback."""
    from gobby.agents.sandbox import compute_sandbox_paths

    if not config.enabled:
        return SandboxLaunch(backend=config.backend, enforced=False)

    paths = compute_sandbox_paths(
        config,
        workspace_path,
        daemon_port,
        gobby_websocket_port=websocket_port,
        provider=provider,
        api_base=api_base,
        env=env,
    )
    if config.backend == "provider-native":
        if resolver is None:
            raise SrtRuntimeError(f"{provider} does not support provider-native sandboxing")
        provider_args, provider_env = resolver.resolve(config, paths)
        if not provider_args and not provider_env:
            raise SrtRuntimeError(f"{provider} did not activate its provider-native sandbox")
        return SandboxLaunch(
            backend="provider-native",
            enforced=True,
            provider_args=provider_args,
            provider_env=provider_env,
        )

    if config.allow_network:
        raise SrtRuntimeError(
            "SRT does not accept unrestricted network access; configure explicit "
            "allowed_domains or enable a scoped Git/package capability"
        )

    installation = verify_srt_installation()
    policy_dir = secure_policy_directory(run_id)
    temp_dir = policy_dir / "tmp"
    temp_dir.mkdir(mode=0o700, exist_ok=True)
    temp_dir.chmod(0o700)
    temp_path = str(temp_dir.resolve())
    paths.read_paths.append(temp_path)
    paths.write_paths.append(temp_path)
    settings = render_srt_settings(paths)
    policy_bytes = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    policy_hash = hashlib.sha256(policy_bytes).hexdigest()
    policy_path = policy_dir / "settings.json"
    violation_path = policy_dir / "violations.jsonl"
    _write_private_file(policy_path, policy_bytes)
    _write_private_file(violation_path, b"")
    launch = SandboxLaunch(
        backend="srt",
        enforced=True,
        runtime_version=SRT_VERSION,
        policy_hash=policy_hash,
        policy_path=str(policy_path),
        violation_path=str(violation_path),
        provider_env={"CLAUDE_CODE_TMPDIR": temp_path},
        node_path=str(installation.node),
        runner_path=str(installation.runner),
    )
    await _preflight_srt(launch, workspace_path, {**env, **launch.provider_env})
    return launch


def _write_private_file(path: Path, content: bytes) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        temp_path.unlink(missing_ok=True)


async def _preflight_srt(
    launch: SandboxLaunch, workspace_path: str, env: Mapping[str, str]
) -> None:
    command = launch.wrap([])[:-1] + ["--preflight"]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace_path,
        env=dict(env),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise SrtRuntimeError("managed SRT preflight timed out") from exc
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise SrtRuntimeError(f"managed SRT preflight failed: {detail or 'unknown error'}")
