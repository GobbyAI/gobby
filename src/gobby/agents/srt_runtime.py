"""Pinned Sandbox Runtime launch preparation for managed CLI agents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from gobby.agents.provider_capabilities import provider_capabilities
from gobby.agents.sandbox_policy import (
    SRT_SETTINGS_RELATIVE_PATH,
    SRT_VIOLATIONS_RELATIVE_PATH,
    assert_sensitive_path_contract,
    canonical_path,
    prepare_sandbox_run_paths,
    previous_run_write_paths,
    srt_mux_tmpdir,
)
from gobby.paths import get_gobby_home
from gobby.sync.jsonl_io import export_file_lock
from gobby.utils.dependency_requirements import (
    SRT_RELEASE,
    node_dependency_status,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

from gobby.agents.sandbox import (
    ResolvedSandboxPaths,
    SandboxConfig,
    SandboxResolver,
    preflight_provider_native_settings,
)

logger = logging.getLogger(__name__)


class SrtRuntimeError(RuntimeError):
    """Raised when the pinned runtime or policy cannot be used safely."""


_CONTENT_MANIFEST_NAME = "content-manifest.json"


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
    provider_executable: str | None = None
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
        if not all(
            (
                self.provider_executable,
                self.node_path,
                self.runner_path,
                self.policy_path,
                self.violation_path,
            )
        ):
            raise SrtRuntimeError("SRT launch metadata is incomplete")
        assert self.provider_executable is not None
        assert self.node_path is not None
        assert self.runner_path is not None
        assert self.policy_path is not None
        assert self.violation_path is not None
        provider_command = list(command)
        if provider_command:
            provider_command[0] = self.provider_executable
        return [
            self.node_path,
            self.runner_path,
            "--settings",
            self.policy_path,
            "--violations",
            self.violation_path,
            "--",
            *provider_command,
        ]

    def metadata(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "enforced": self.enforced,
            "runtime_version": self.runtime_version,
            "policy_hash": self.policy_hash,
            "policy_path": self.policy_path,
            "violation_path": self.violation_path,
            "provider_executable": self.provider_executable,
        }


def srt_install_root() -> Path:
    return get_gobby_home() / "tools" / "srt" / SRT_RELEASE.version


def build_srt_content_manifest(root: Path) -> dict[str, str]:
    """Hash every file or link used by the managed SRT dependency graph."""
    candidates = [root / "runner.mjs", root / "package-lock.json"]
    node_modules = root / "node_modules"
    if node_modules.exists():
        candidates.extend(node_modules.rglob("*"))
    manifest: dict[str, str] = {}
    for path in sorted(candidates, key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            manifest[relative] = f"link:{os.readlink(path)}"
        elif path.is_file():
            manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def write_srt_content_manifest(root: Path) -> None:
    """Write the deterministic package-content manifest before promotion."""
    payload = json.dumps(build_srt_content_manifest(root), sort_keys=True, separators=(",", ":"))
    (root / _CONTENT_MANIFEST_NAME).write_text(payload, encoding="utf-8")


def make_srt_installation_immutable(root: Path) -> None:
    """Remove write bits from the complete promoted SRT tree."""
    paths = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for path in paths:
        if path.is_symlink():
            continue
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _verify_srt_content(root: Path, manifest: dict[str, str]) -> None:
    actual = build_srt_content_manifest(root)
    if actual != manifest:
        raise SrtRuntimeError("managed SRT content manifest mismatch; rerun `gobby install`")
    protected = [
        root,
        root / "receipt.json",
        root / _CONTENT_MANIFEST_NAME,
        *(root / relative for relative in manifest),
        *((root / "node_modules").rglob("*")),
    ]
    for path in protected:
        if path.is_symlink():
            continue
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise SrtRuntimeError("managed SRT content manifest mismatch") from exc
        if mode & 0o222:
            raise SrtRuntimeError(f"managed SRT content is writable: {path.relative_to(root)}")


@contextmanager
def srt_install_lock() -> Iterator[None]:
    """Serialize installation and verification of the pinned SRT version."""
    with export_file_lock(srt_install_root()):
        yield


def _raise_srt_lockout(
    reason: str,
    *,
    run_id: str | None,
    provider: str | None,
    policy_hash: str | None,
    cause: BaseException | None = None,
) -> NoReturn:
    logger.warning(
        "Managed SRT validation failed closed",
        extra={
            "run_id": run_id,
            "provider": provider,
            "policy_hash": policy_hash,
            "lockout_reason": reason,
        },
    )
    error = SrtRuntimeError(reason)
    if cause is not None:
        raise error from cause
    raise error


def verify_srt_installation_locked(
    *,
    run_id: str | None = None,
    provider: str | None = None,
    policy_hash: str | None = None,
) -> SrtInstallation:
    """Verify the pinned runtime while the caller holds :func:`srt_install_lock`."""
    try:
        root = srt_install_root().resolve(strict=False)
    except OSError as exc:
        _raise_srt_lockout(
            f"managed SRT {SRT_RELEASE.version} install root is invalid; rerun `gobby install`",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
            cause=exc,
        )
    receipt_path = root / "receipt.json"
    runner = root / "runner.mjs"
    bundled_runner = Path(__file__).with_name("srt_runner.mjs")
    package_json = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "package.json"
    content_manifest = root / _CONTENT_MANIFEST_NAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        package = json.loads(package_json.read_text(encoding="utf-8"))
        runner_bytes = runner.read_bytes()
        bundled_runner_bytes = bundled_runner.read_bytes()
        lockfile_bytes = (root / "package-lock.json").read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        _raise_srt_lockout(
            f"managed SRT {SRT_RELEASE.version} is missing or invalid; rerun `gobby install`",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
            cause=exc,
        )
    try:
        manifest = json.loads(content_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _raise_srt_lockout(
            "managed SRT content manifest is missing or invalid; rerun `gobby install`",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
            cause=exc,
        )
    if not isinstance(receipt, dict) or not isinstance(package, dict):
        _raise_srt_lockout(
            f"managed SRT {SRT_RELEASE.version} receipt or package metadata is invalid",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
        )
    if not isinstance(manifest, dict):
        _raise_srt_lockout(
            "managed SRT content manifest is invalid",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
        )
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in manifest.items()):
        _raise_srt_lockout(
            "managed SRT content manifest is invalid",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
        )
    expected_receipt = SRT_RELEASE.receipt_fields()
    if any(receipt.get(key) != value for key, value in expected_receipt.items()):
        _raise_srt_lockout(
            "managed SRT receipt does not match Gobby's pinned runtime",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
        )
    if package.get("name") != SRT_RELEASE.package or package.get("version") != SRT_RELEASE.version:
        _raise_srt_lockout(
            "managed SRT package identity does not match its receipt",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
        )
    runner_sha256 = hashlib.sha256(runner_bytes).hexdigest()
    if runner_sha256 != SRT_RELEASE.runner_sha256:
        _raise_srt_lockout(
            "managed SRT runner checksum mismatch",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
        )
    if hashlib.sha256(bundled_runner_bytes).hexdigest() != SRT_RELEASE.runner_sha256:
        _raise_srt_lockout(
            "managed SRT runner does not match this Gobby installation",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
        )
    if hashlib.sha256(lockfile_bytes).hexdigest() != SRT_RELEASE.lockfile_sha256:
        _raise_srt_lockout(
            "managed SRT lockfile checksum mismatch",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
        )
    try:
        _verify_srt_content(root, manifest)
    except SrtRuntimeError as exc:
        _raise_srt_lockout(
            str(exc),
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
            cause=exc,
        )

    node_status = node_dependency_status()
    if node_status.state != "healthy" or node_status.path is None:
        _raise_srt_lockout(
            node_status.error or "Node.js version could not be verified",
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
        )
    node = Path(node_status.path)
    return SrtInstallation(root=root, node=node, runner=runner.resolve(), package_json=package_json)


def verify_srt_installation(
    *,
    run_id: str | None = None,
    provider: str | None = None,
    policy_hash: str | None = None,
) -> SrtInstallation:
    """Serialize and verify the pinned package, receipt, runner, and Node runtime."""
    with srt_install_lock():
        return verify_srt_installation_locked(
            run_id=run_id,
            provider=provider,
            policy_hash=policy_hash,
        )


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


def _preflight_provider_native(
    provider: str,
    provider_args: list[str],
    paths: ResolvedSandboxPaths,
) -> None:
    """Verify the effective provider policy carries every sensitive exclusion."""
    if provider != "claude" or len(provider_args) < 2 or provider_args[0] != "--settings":
        raise SrtRuntimeError(f"{provider} cannot prove the sensitive-root contract")
    try:
        settings = json.loads(provider_args[1])
        preflight_provider_native_settings(provider, settings, paths)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SrtRuntimeError(f"{provider} emitted an unverifiable sensitive-root policy") from exc


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

    if config.backend == "srt" and config.allow_network:
        rejected_policy = json.dumps(
            {
                "provider": provider,
                "sandbox": config.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        _raise_srt_lockout(
            "SRT does not accept unrestricted network access; configure explicit "
            "allowed_domains or enable a scoped Git/package capability",
            run_id=run_id,
            provider=provider,
            policy_hash=hashlib.sha256(rejected_policy).hexdigest(),
        )

    capabilities = provider_capabilities(provider)
    if config.backend == "provider-native" and (
        resolver is None or not capabilities.sensitive_path_enforcement
    ):
        raise SrtRuntimeError(f"{provider} cannot prove the sensitive-root contract")

    provider_executable = (
        _resolve_provider_executable(provider, env) if config.backend == "srt" else None
    )

    run_paths = prepare_sandbox_run_paths(run_id, env)
    run_environment = run_paths.environment(provider)
    prompt_file = env.get("GOBBY_PROMPT_FILE")
    if prompt_file and Path(prompt_file).is_file():
        run_prompt = run_paths.assets / "prompt.md"
        shutil.copyfile(prompt_file, run_prompt)
        run_prompt.chmod(0o600)
        run_environment["GOBBY_PROMPT_FILE"] = str(run_prompt)
    superseded_writes = previous_run_write_paths(env)
    retained_writes = [
        path
        for path in config.extra_write_paths
        if canonical_path(path, base=Path(workspace_path)) not in superseded_writes
    ]
    effective_config = config.model_copy(
        update={
            "extra_write_paths": [*retained_writes, *(str(path) for path in run_paths.writable)]
        }
    )
    effective_env = {**env, **run_environment}
    paths = compute_sandbox_paths(
        effective_config,
        workspace_path,
        daemon_port,
        gobby_websocket_port=websocket_port,
        provider=provider,
        provider_executable=provider_executable,
        api_base=api_base,
        env=effective_env,
    )
    paths.read_paths.append(str(run_paths.assets.resolve()))
    assert_sensitive_path_contract(paths.read_paths, paths.write_paths)
    if config.backend == "provider-native":
        assert resolver is not None
        provider_args, provider_env = resolver.resolve(effective_config, paths)
        if not provider_args and not provider_env:
            raise SrtRuntimeError(f"{provider} did not activate its provider-native sandbox")
        _preflight_provider_native(provider, provider_args, paths)
        return SandboxLaunch(
            backend="provider-native",
            enforced=True,
            provider_args=provider_args,
            provider_env={**run_environment, **provider_env},
        )

    settings = render_srt_settings(paths)
    policy_bytes = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    policy_hash = hashlib.sha256(policy_bytes).hexdigest()
    # Off the loop: verification rehashes the whole managed tree -- rglob over
    # node_modules, SHA-256 of every file, then a stat of each one again, 826 files
    # and 44ms here warm and at rest -- and takes a blocking file lock first. Inline
    # it made every spawn a multi-hundred-millisecond stall, which is what the
    # loop-lag watchdog caught as _verify_srt_content -> Path.stat (#20841).
    installation = await asyncio.to_thread(
        verify_srt_installation,
        run_id=run_id,
        provider=provider,
        policy_hash=policy_hash,
    )
    policy_path = run_paths.root / SRT_SETTINGS_RELATIVE_PATH
    violation_path = run_paths.root / SRT_VIOLATIONS_RELATIVE_PATH
    _write_private_file(policy_path, policy_bytes)
    _write_private_file(violation_path, b"")
    launch = SandboxLaunch(
        backend="srt",
        enforced=True,
        runtime_version=SRT_RELEASE.version,
        policy_hash=policy_hash,
        policy_path=str(policy_path),
        violation_path=str(violation_path),
        provider_env={**run_environment, "GOBBY_SRT_TMPDIR": str(srt_mux_tmpdir())},
        provider_executable=provider_executable,
        node_path=str(installation.node),
        runner_path=str(installation.runner),
    )
    try:
        await _preflight_srt(launch, workspace_path, {**env, **launch.provider_env})
    except SrtRuntimeError as exc:
        logger.warning(
            "Managed SRT preflight failed closed",
            extra={
                "run_id": run_id,
                "provider": provider,
                "policy_hash": policy_hash,
                "lockout_reason": str(exc),
            },
        )
        raise
    return launch


def _resolve_provider_executable(provider: str, env: Mapping[str, str]) -> str:
    """Resolve one SRT provider to the exact executable used by the sandbox."""
    try:
        search_path = env["PATH"]
    except KeyError as exc:
        raise SrtRuntimeError(f"{provider} executable resolution requires PATH") from exc
    executable = shutil.which(provider, path=search_path)
    if executable is None:
        raise SrtRuntimeError(f"{provider} executable not found in PATH")
    try:
        return str(Path(executable).resolve(strict=True))
    except OSError as exc:
        raise SrtRuntimeError(
            f"Failed to resolve {provider} executable {executable!r}: {exc}"
        ) from exc


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
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=workspace_path,
            env=dict(env),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise SrtRuntimeError("managed SRT preflight could not start") from exc
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise SrtRuntimeError("managed SRT preflight timed out") from exc
    except OSError as exc:
        raise SrtRuntimeError("managed SRT preflight execution failed") from exc
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise SrtRuntimeError(f"managed SRT preflight failed: {detail or 'unknown error'}")
