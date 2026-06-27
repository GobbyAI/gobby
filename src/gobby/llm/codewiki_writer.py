"""Read-only CodeWiki page writer service."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gobby.ai import (
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    build_daemon_ai_capability_registry,
)
from gobby.ai._text_generation_adapters import _cleanup_cli_process, _extend_reasoning_args
from gobby.ai._text_generation_contracts import TextGenerationRequest
from gobby.ai._text_generation_helpers import _compose_prompt
from gobby.config.app import DaemonConfig
from gobby.config.feature_base import (
    DEFAULT_PROFILE_CANDIDATES,
    FeatureCandidateConfig,
    FeatureCandidateInput,
    FeatureProfile,
    candidate_runtime_entries,
    parse_feature_candidate,
)

DEFAULT_CODEWIKI_WRITER_TIMEOUT_SECONDS = 240.0
SUPPORTED_CODEWIKI_WRITER_PROVIDER = "codex"


@dataclass(frozen=True, kw_only=True)
class CodeWikiWriterRequest:
    """One CodeWiki page-writing request."""

    prompt: str
    system_prompt: str | None
    cwd: str
    profile: str | None = None
    candidates: tuple[FeatureCandidateInput, ...] = ()
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    reasoning_effort: str | None = None
    page_kind: str | None = None


@dataclass(frozen=True)
class CodeWikiWriterResult:
    """Successful CodeWiki writer response."""

    text: str
    provider: str
    model: str
    usage: dict[str, int] | None
    elapsed_ms: int
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return an API-safe response body."""
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "usage": self.usage,
            "elapsed_ms": self.elapsed_ms,
            "diagnostics": self.diagnostics,
        }


class CodeWikiWriterError(Exception):
    """Structured non-retryable writer failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostics = dict(diagnostics or {})

    def to_dict(self) -> dict[str, Any]:
        """Return an API-safe error body."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": False,
                "diagnostics": self.diagnostics,
            }
        }


@dataclass(frozen=True)
class _SelectedWriter:
    binding: CapabilityBinding
    model: str
    profile: str | None
    reasoning_effort: str | None
    diagnostics: dict[str, Any]


type CodeWikiCommandRunner = Callable[
    [Sequence[str], Path, float, Mapping[str, str]],
    Awaitable[None],
]


class CodeWikiWriterService:
    """Dedicated CodeWiki prose writer backed by read-only Codex exec."""

    def __init__(
        self,
        registry: AICapabilityRegistry,
        *,
        profile_defaults: Mapping[FeatureProfile, Sequence[FeatureCandidateInput]] | None = None,
        command_path: str | None = None,
        env: Mapping[str, str] | None = None,
        runner: CodeWikiCommandRunner | None = None,
        tech_writer_loader: Callable[[], str] | None = None,
    ) -> None:
        self._registry = registry
        self._profile_defaults = dict(profile_defaults or {})
        self._command_path = command_path
        self._env = dict(env or {})
        self._runner = runner or _run_codex_codewiki_writer
        self._tech_writer_loader = tech_writer_loader or _load_tech_writer_methodology

    async def write(self, request: CodeWikiWriterRequest) -> CodeWikiWriterResult:
        """Generate CodeWiki page prose."""
        started = time.perf_counter()
        repo_root = self._validate_request(request)
        selected = self._select_writer(request)
        prompt = self._compose_writer_prompt(request)
        timeout_seconds = request.timeout_seconds or DEFAULT_CODEWIKI_WRITER_TIMEOUT_SECONDS

        with tempfile.TemporaryDirectory(prefix="gobby-codewiki-writer-") as temp_dir:
            output_path = Path(temp_dir) / "last-message.txt"
            command = self._build_codex_command(
                request,
                selected,
                repo_root=repo_root,
                output_path=output_path,
                prompt=prompt,
            )
            try:
                await self._runner(command, repo_root, timeout_seconds, self._env)
            except TimeoutError as exc:
                raise CodeWikiWriterError(
                    "timeout",
                    f"CodeWiki writer timed out after {timeout_seconds:g}s",
                    diagnostics=selected.diagnostics,
                ) from exc
            except RuntimeError as exc:
                raise CodeWikiWriterError(
                    "provider_execution_failed",
                    str(exc),
                    diagnostics=selected.diagnostics,
                ) from exc

            text = output_path.read_text(encoding="utf-8").strip()

        if not text:
            raise CodeWikiWriterError(
                "empty_output",
                "CodeWiki writer produced empty output",
                diagnostics=selected.diagnostics,
            )

        diagnostics = {
            **selected.diagnostics,
            "cwd": str(repo_root),
            "page_kind": request.page_kind,
            "timeout_seconds": timeout_seconds,
        }
        return CodeWikiWriterResult(
            text=text,
            provider=selected.binding.provider,
            model=selected.model,
            usage=None,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            diagnostics=diagnostics,
        )

    @staticmethod
    def _validate_request(request: CodeWikiWriterRequest) -> Path:
        prompt = request.prompt.strip()
        if not prompt:
            raise CodeWikiWriterError("invalid_request", "prompt is required")
        if bool(request.profile) == bool(request.candidates):
            raise CodeWikiWriterError(
                "invalid_request",
                "exactly one of profile or candidates is required",
            )
        if request.timeout_seconds is not None and request.timeout_seconds <= 0:
            raise CodeWikiWriterError("invalid_request", "timeout_seconds must be positive")
        repo_root = Path(request.cwd).expanduser().resolve()
        if not repo_root.is_dir():
            raise CodeWikiWriterError(
                "invalid_request",
                "cwd must be an existing directory",
                diagnostics={"cwd": request.cwd},
            )
        return repo_root

    def _select_writer(self, request: CodeWikiWriterRequest) -> _SelectedWriter:
        candidates = self._candidate_entries(request)
        rejected: list[dict[str, str]] = []
        for candidate in candidates:
            provider, model = parse_feature_candidate(candidate)
            reasoning_effort = request.reasoning_effort or candidate.reasoning_effort
            label = f"{provider}/{model}"
            if provider != SUPPORTED_CODEWIKI_WRITER_PROVIDER:
                rejected.append({"candidate": label, "reason": "unsupported_provider"})
                continue
            binding = self._registry.binding(AICapability.TEXT_GENERATE, provider)
            if binding is None:
                rejected.append({"candidate": label, "reason": "missing_binding"})
                continue
            if not binding.available:
                rejected.append({"candidate": label, "reason": binding.reason or "unavailable"})
                continue
            if not binding.supports_model(model) and not binding.accepts_explicit_model_override(
                model
            ):
                rejected.append({"candidate": label, "reason": "unsupported_model"})
                continue
            return _SelectedWriter(
                binding=binding,
                model=model,
                profile=request.profile,
                reasoning_effort=reasoning_effort,
                diagnostics={
                    "candidate": label,
                    "profile": request.profile,
                    "rejected_candidates": rejected,
                },
            )

        raise CodeWikiWriterError(
            "unsupported_provider_model",
            "No supported CodeWiki writer candidate is available",
            diagnostics={"rejected_candidates": rejected},
        )

    def _candidate_entries(
        self, request: CodeWikiWriterRequest
    ) -> tuple[FeatureCandidateConfig, ...]:
        if request.candidates:
            return candidate_runtime_entries(request.candidates, profile=request.profile)
        if request.profile is None:
            raise CodeWikiWriterError(
                "invalid_request", "profile is required when candidates are empty"
            )
        profile = FeatureProfile(request.profile)
        candidates = self._profile_defaults.get(profile, DEFAULT_PROFILE_CANDIDATES[profile])
        return candidate_runtime_entries(candidates, profile=profile)

    def _compose_writer_prompt(self, request: CodeWikiWriterRequest) -> str:
        system_prompt = "\n\n".join(
            part
            for part in (
                _codewiki_writer_system_prompt(request.page_kind),
                _tech_writer_section(self._tech_writer_loader()),
                request.system_prompt,
            )
            if part
        )
        return _compose_prompt(
            TextGenerationRequest(prompt=request.prompt, system_prompt=system_prompt)
        )

    def _build_codex_command(
        self,
        request: CodeWikiWriterRequest,
        selected: _SelectedWriter,
        *,
        repo_root: Path,
        output_path: Path,
        prompt: str,
    ) -> list[str]:
        command = [
            self._resolve_command_path(),
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--cd",
            str(repo_root),
            "--output-last-message",
            str(output_path),
        ]
        if selected.model:
            command.extend(["--model", selected.model])
        _extend_reasoning_args(command, selected.binding.provider, selected.reasoning_effort)
        if request.max_tokens is not None:
            command.extend(["-c", f"model_max_output_tokens={request.max_tokens}"])
        command.append(prompt)
        return command

    def _resolve_command_path(self) -> str:
        path = self._command_path or shutil.which("codex")
        if not path:
            raise CodeWikiWriterError(
                "unsupported_provider_model",
                "Codex CLI not found in PATH",
                diagnostics={"provider": SUPPORTED_CODEWIKI_WRITER_PROVIDER},
            )
        return path


def build_codewiki_writer_service(config: DaemonConfig) -> CodeWikiWriterService:
    """Build the daemon CodeWiki writer service."""
    return CodeWikiWriterService(
        build_daemon_ai_capability_registry(config),
        profile_defaults=config.ai.generation.profile_defaults,
    )


async def _run_codex_codewiki_writer(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    env_overrides: Mapping[str, str],
) -> None:
    env = os.environ.copy()
    env.update(env_overrides)
    env["GOBBY_HOOKS_DISABLED"] = "1"
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await _cleanup_cli_process("Codex CodeWiki writer", process, reason="timeout")
        raise
    except asyncio.CancelledError:
        await _cleanup_cli_process("Codex CodeWiki writer", process, reason="cancellation")
        raise

    if process.returncode:
        message = _decode(stderr).strip() or _decode(stdout).strip()
        raise RuntimeError(
            f"Codex CodeWiki writer failed with exit code {process.returncode}: {message}"
        )


def _decode(value: bytes | str) -> str:
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _codewiki_writer_system_prompt(page_kind: str | None) -> str:
    page_label = page_kind or "codewiki page"
    return (
        "You are a dedicated CodeWiki narrative writer. Write only the requested "
        f"{page_label} prose. You may inspect the repository using read-only search "
        "and file-reading commands such as rg, gcode search, gcode outline, gcode "
        "symbol, sed, head, tail, and cat. Do not modify files, create tasks, spawn "
        "agents, call Gobby task or agent APIs, or run commands that write to disk. "
        "Ground concrete claims in repository evidence. Return the page body only."
    )


def _tech_writer_section(content: str) -> str:
    return f"Tech-writer methodology to apply, not an agent to spawn:\n\n{content.strip()}"


def _load_tech_writer_methodology() -> str:
    path = (
        Path(__file__).resolve().parents[1]
        / "install"
        / "shared"
        / "skills"
        / "tech-writer"
        / "SKILL.md"
    )
    return path.read_text(encoding="utf-8")


__all__ = [
    "CodeWikiWriterError",
    "CodeWikiWriterRequest",
    "CodeWikiWriterResult",
    "CodeWikiWriterService",
    "build_codewiki_writer_service",
]
