"""Concrete daemon text generation adapters."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import shlex
import shutil
import signal
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload

from gobby.agents.provider_capabilities import provider_reasoning_flag
from gobby.agents.reasoning import normalize_reasoning_effort
from gobby.ai._agy_models import normalize_agy_model_selection, resolve_agy_display
from gobby.ai._text_generation_contracts import TextGenerateAdapter, TextGenerationRequest
from gobby.ai._text_generation_helpers import (
    _compose_prompt,
    _decode,
    _json_request,
    _parse_json_text,
    _with_one_shot_directive,
)
from gobby.ai.codex_endpoint import codex_model_config_override
from gobby.config.app import DaemonConfig
from gobby.llm.textgen_cwd import neutral_textgen_cwd

if TYPE_CHECKING:
    from gobby.llm.base import LLMTextResult

logger = logging.getLogger("gobby.ai.text_generation")
_CLI_PROCESS_CLEANUP_TIMEOUT_SECONDS = 2.0
_DROID_AUTH_ERROR_HINT = (
    "Droid ran in an isolated temporary home; set FACTORY_API_KEY for headless auth "
    "without reusing your real Droid session state."
)
_DROID_FACTORY_EXCLUDED_NAMES = frozenset(
    {
        "sessions",
        "logs",
        "temp",
        "telemetry",
        "background-processes.json",
        "background-tasks.json",
        "history.json",
    }
)
_DROID_FACTORY_ALLOWED_TOP_LEVEL_DIRS = frozenset({"certs", "droids", "hooks"})
_DROID_FACTORY_ALLOWED_CACHE_DIRS = frozenset({"certs"})
_DROID_FACTORY_ALLOWED_PLUGIN_FILES = frozenset(
    {
        ("plugins", "installed_plugins.json"),
        ("plugins", "known_marketplaces.json"),
    }
)
_DROID_FACTORY_ALLOWED_PLUGIN_DIRS = frozenset({("plugins", "marketplaces")})
_DROID_FACTORY_ALLOWED_FILE_KEYWORDS = frozenset(
    {"auth", "cert", "config", "credential", "hint", "host", "mcp", "setting", "token"}
)
_AGY_ERROR_STDOUT_PREFIX = "Error:"


def _extend_reasoning_args(command: list[str], provider: str, reasoning_effort: str | None) -> None:
    reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    if not reasoning_effort:
        return
    match provider_reasoning_flag(provider):
        case "codex-config":
            command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        case "reasoning-effort":
            command.extend(["--reasoning-effort", reasoning_effort])
        case None:
            return


@dataclass(frozen=True)
class _QwenOpenAIEndpoint:
    """OpenAI-compatible endpoint settings used to isolate Qwen feature calls."""

    api_base: str
    model: str
    api_key: str | None = None


class ClaudeTextGenerateAdapter:
    """Native text_generate adapter backed by Claude SDK primitives."""

    def __init__(self, config: DaemonConfig) -> None:
        from gobby.llm.claude import ClaudeLLMProvider

        self._provider = ClaudeLLMProvider(config)

    async def generate(self, request: TextGenerationRequest) -> LLMTextResult:
        return await self._provider.generate_text_result(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
            max_tokens=request.max_tokens,
            reasoning_effort=request.reasoning_effort,
            caller=request.caller,
        )

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        if request.json_schema is None:
            raise ValueError("Claude JSON generation requires a JSON schema")
        return await self._provider.generate_json(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
            json_schema=request.json_schema,
            reasoning_effort=request.reasoning_effort,
            caller=request.caller,
        )


class LocalTextGenerateAdapter:
    """Native text_generate adapter backed by a local OpenAI-compatible endpoint."""

    def __init__(self, config: DaemonConfig, endpoint_name: str) -> None:
        from gobby.llm.local import LocalLLMProvider

        self._provider = LocalLLMProvider(config, endpoint_name=endpoint_name)

    async def generate(self, request: TextGenerationRequest) -> LLMTextResult:
        return await self._provider.generate_text_result(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
            max_tokens=request.max_tokens,
            reasoning_effort=request.reasoning_effort,
            caller=request.caller,
            images=request.images,
        )

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        return await self._provider.generate_json(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
            max_tokens=request.max_tokens,
            reasoning_effort=request.reasoning_effort,
            caller=request.caller,
        )


@overload
async def _run_cli_text_generation_command(
    provider_name: str,
    command: Sequence[str],
    *,
    neutral_cwd: Path,
    timeout_seconds: float,
    env_overrides: Mapping[str, str],
    stdin_input: str | None = None,
    accepted_exit_codes: None = None,
) -> str: ...


@overload
async def _run_cli_text_generation_command(
    provider_name: str,
    command: Sequence[str],
    *,
    neutral_cwd: Path,
    timeout_seconds: float,
    env_overrides: Mapping[str, str],
    stdin_input: str | None = None,
    accepted_exit_codes: Collection[int],
) -> tuple[str, str, int]: ...


async def _run_cli_text_generation_command(
    provider_name: str,
    command: Sequence[str],
    *,
    neutral_cwd: Path,
    timeout_seconds: float,
    env_overrides: Mapping[str, str],
    stdin_input: str | None = None,
    accepted_exit_codes: Collection[int] | None = None,
) -> str | tuple[str, str, int]:
    # One-shot text generation never runs in the project directory: ``neutral_cwd``
    # is a per-call temp dir owned by the calling adapter (see neutral_textgen_cwd).
    # ``request.cwd`` is intentionally never threaded here — that prevents project
    # context/hooks from loading and adding a large variable startup tax.
    env = os.environ.copy()
    env.update(env_overrides)
    env["GOBBY_HOOKS_DISABLED"] = "1"
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=(asyncio.subprocess.PIPE if stdin_input is not None else asyncio.subprocess.DEVNULL),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(neutral_cwd),
        env=env,
        start_new_session=True,
    )
    try:
        communicate_coro = (
            process.communicate(input=stdin_input.encode("utf-8"))
            if stdin_input is not None
            else process.communicate()
        )
        stdout, stderr = await asyncio.wait_for(
            communicate_coro,
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        await _cleanup_cli_process(provider_name, process, reason="timeout")
        raise RuntimeError(
            f"{provider_name} CLI timed out after {timeout_seconds:g}s: {shlex.join(command)}"
        ) from exc
    except asyncio.CancelledError:
        await _cleanup_cli_process(provider_name, process, reason="cancellation")
        raise

    returncode = process.returncode or 0
    stdout_text = _decode(stdout).strip()
    stderr_text = _decode(stderr).strip()
    if returncode and (accepted_exit_codes is None or returncode not in accepted_exit_codes):
        message = stderr_text or stdout_text
        raise RuntimeError(f"{provider_name} CLI failed with exit code {returncode}: {message}")
    if accepted_exit_codes is not None:
        return stdout_text, stderr_text, returncode
    return stdout_text


def _signal_cli_process_group(process: Any, sig: signal.Signals) -> bool:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int):
        return False
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)
        return True
    return False


async def _cleanup_cli_process(provider_name: str, process: Any, *, reason: str) -> None:
    if process.returncode is not None:
        logger.debug(
            "%s CLI process cleanup skipped after %s; process already exited with code %s",
            provider_name,
            reason,
            process.returncode,
        )
        return

    if not _signal_cli_process_group(process, signal.SIGTERM):
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            with contextlib.suppress(ProcessLookupError):
                terminate()

    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=_CLI_PROCESS_CLEANUP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        if not _signal_cli_process_group(process, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=_CLI_PROCESS_CLEANUP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "%s CLI process cleanup failed after %s; process did not exit within %.1fs",
                provider_name,
                reason,
                _CLI_PROCESS_CLEANUP_TIMEOUT_SECONDS * 2,
            )
            return
        except Exception:
            logger.warning(
                "%s CLI process forced cleanup failed after %s",
                provider_name,
                reason,
                exc_info=True,
            )
            return
    except Exception:
        logger.warning(
            "%s CLI process cleanup failed after %s",
            provider_name,
            reason,
            exc_info=True,
        )
        return

    logger.debug(
        "%s CLI process cleanup completed after %s; returncode=%s",
        provider_name,
        reason,
        process.returncode,
    )


class _QwenCLITextGenerateAdapter:
    """One-shot text_generate adapter for Qwen positional headless CLI mode."""

    def __init__(
        self,
        *,
        command_path: str | None = None,
        timeout_seconds: float = 600.0,
        env: Mapping[str, str] | None = None,
        openai_endpoints: Mapping[str, Any] | None = None,
    ) -> None:
        self._command_path = command_path
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})
        self._openai_endpoints = _normalize_qwen_openai_endpoints(openai_endpoints or {})

    def build_command(self, request: TextGenerationRequest) -> list[str]:
        path = self._command_path or shutil.which("qwen")
        if not path:
            raise FileNotFoundError("Qwen CLI not found in PATH")

        command = [
            path,
            "--bare",
            "--chat-recording=false",
            "--max-tool-calls",
            "0",
            "--max-session-turns",
            "1",
            "--output-format",
            "text",
        ]
        endpoint = self._select_openai_endpoint(request)
        if endpoint is not None:
            command.extend(["--auth-type", "openai", "--openai-base-url", endpoint.api_base])
        model = endpoint.model if endpoint is not None else request.model
        if model:
            command.extend(["--model", model])
        command.append(_compose_prompt(request))
        return command

    async def generate(self, request: TextGenerationRequest) -> str:
        request = _with_one_shot_directive(request)
        env = dict(self._env)
        endpoint = self._select_openai_endpoint(request)
        if endpoint is not None:
            env["OPENAI_API_KEY"] = endpoint.api_key or "not-needed"
            env["OPENAI_BASE_URL"] = endpoint.api_base
            env["OPENAI_MODEL"] = endpoint.model
        with neutral_textgen_cwd() as cwd:
            return await _run_cli_text_generation_command(
                "Qwen",
                self.build_command(request),
                neutral_cwd=cwd,
                timeout_seconds=self._timeout_seconds,
                env_overrides=env,
            )

    def _select_openai_endpoint(self, request: TextGenerationRequest) -> _QwenOpenAIEndpoint | None:
        if not self._openai_endpoints:
            return None
        if request.model:
            for endpoint in self._openai_endpoints.values():
                if request.model == endpoint.model:
                    return endpoint
        if len(self._openai_endpoints) == 1:
            return next(iter(self._openai_endpoints.values()))
        return None


def _normalize_qwen_openai_endpoints(
    endpoints: Mapping[str, Any],
) -> dict[str, _QwenOpenAIEndpoint]:
    normalized: dict[str, _QwenOpenAIEndpoint] = {}
    for name, endpoint in endpoints.items():
        api_base = str(getattr(endpoint, "api_base", "") or "").strip()
        model = str(getattr(endpoint, "model", "") or "").strip()
        if not api_base or not model:
            continue
        api_key = getattr(endpoint, "api_key", None)
        normalized[name] = _QwenOpenAIEndpoint(
            api_base=api_base,
            model=model,
            api_key=str(api_key).strip() if api_key else None,
        )
    return normalized


class _GrokCLITextGenerateAdapter:
    """One-shot text_generate adapter for Grok top-level headless CLI mode."""

    def __init__(
        self,
        *,
        command_path: str | None = None,
        timeout_seconds: float = 600.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._command_path = command_path
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})

    def build_command(self, request: TextGenerationRequest, *, leader_socket: Path) -> list[str]:
        path = self._command_path or shutil.which("grok")
        if not path:
            raise FileNotFoundError("Grok CLI not found in PATH")

        command = [
            path,
            "--output-format",
            "plain",
            "--permission-mode",
            "plan",
            "--max-turns",
            "1",
            "--no-memory",
            "--no-subagents",
            "--disable-web-search",
            "--deny",
            "*",
            "--disallowed-tools",
            "*",
            "--leader-socket",
            str(leader_socket),
        ]
        if request.model:
            command.extend(["--model", request.model])
        _extend_reasoning_args(command, "grok", request.reasoning_effort)
        command.extend(["--single", _compose_prompt(request)])
        return command

    async def generate(self, request: TextGenerationRequest) -> str:
        request = _with_one_shot_directive(request)
        with neutral_textgen_cwd() as cwd:
            leader_socket = cwd / "leader.sock"
            return await _run_cli_text_generation_command(
                "Grok",
                self.build_command(request, leader_socket=leader_socket),
                neutral_cwd=cwd,
                timeout_seconds=self._timeout_seconds,
                env_overrides=self._env,
            )


class AgyCLITextGenerateAdapter:
    """One-shot text_generate adapter for AGY's print-only CLI contract."""

    def __init__(
        self,
        *,
        command_path: str | None = None,
        timeout_seconds: float = 600.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._command_path = command_path
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})

    def build_command(self, request: TextGenerationRequest) -> list[str]:
        path = self._command_path or shutil.which("agy")
        if not path:
            raise FileNotFoundError("AGY CLI not found in PATH")

        command = [
            path,
            "--sandbox",
            "--print-timeout",
            _agy_go_duration(self._timeout_seconds),
        ]
        model, reasoning_effort = normalize_agy_model_selection(
            request.model,
            request.reasoning_effort,
        )
        if request.model is not None:
            if model is None:
                raise RuntimeError("AGY model normalization returned no model")
            command.extend(["--model", resolve_agy_display(model, reasoning_effort)])
        command.extend(["--print", _compose_prompt(request)])
        return command

    async def generate(self, request: TextGenerationRequest) -> str:
        request = _with_one_shot_directive(request)
        with neutral_textgen_cwd() as cwd:
            stdout = await _run_cli_text_generation_command(
                "AGY",
                self.build_command(request),
                neutral_cwd=cwd,
                timeout_seconds=self._timeout_seconds,
                env_overrides=self._env,
            )
        return _validate_agy_stdout(stdout)

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        return _parse_json_text(await self.generate(_json_request(request)))


def _agy_go_duration(timeout_seconds: float) -> str:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("AGY timeout_seconds must be finite and positive")
    return f"{timeout_seconds:g}s"


def _validate_agy_stdout(stdout: str) -> str:
    text = stdout.strip()
    if not text:
        raise RuntimeError("AGY CLI returned empty stdout")
    if text.startswith(_AGY_ERROR_STDOUT_PREFIX):
        raise RuntimeError(f"AGY CLI failed: {text}")
    return text


class CodexCLITextGenerateAdapter:
    """One-shot text_generate adapter for Codex noninteractive CLI mode."""

    def __init__(
        self,
        *,
        command_path: str | None = None,
        timeout_seconds: float = 600.0,
        env: Mapping[str, str] | None = None,
        config_overrides: tuple[str, ...] | None = None,
    ) -> None:
        self._command_path = command_path
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})
        self._config_overrides = tuple(config_overrides or ())

    def _resolve_command_path(self) -> str:
        path = self._command_path or shutil.which("codex")
        if not path:
            raise FileNotFoundError("Codex CLI not found in PATH")
        return path

    def build_command(self, request: TextGenerationRequest, *, output_path: Path) -> list[str]:
        command = [
            self._resolve_command_path(),
            "--ask-for-approval",
            "never",
        ]
        overrides = self._config_overrides
        if request.model:
            overrides = tuple(
                override for override in overrides if not override.startswith("model=")
            )
        for override in overrides:
            command.extend(["-c", override])
        if request.model and self._config_overrides:
            command.extend(["-c", codex_model_config_override(request.model)])
        command.extend(
            [
                "exec",
                "--ephemeral",
                # One-shot generation runs in a neutral temp dir, which is not a Git
                # repository. Codex aborts outside a Git repo unless this flag is set.
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--output-last-message",
                str(output_path),
            ]
        )
        if request.model and not self._config_overrides:
            command.extend(["--model", request.model])
        _extend_reasoning_args(command, "codex", request.reasoning_effort)
        # Intentionally no ``--cd``: one-shot generation runs in a neutral temp dir,
        # never the project directory (avoids the project-context startup tax).
        # Codex ``exec`` reads instructions from stdin when the prompt arg is ``-``.
        # The prompt is fed via stdin (see ``generate``) so large aggregate prompts
        # do not overflow ARG_MAX as a command-line argument (#17457).
        command.append("-")
        return command

    async def generate(self, request: TextGenerationRequest) -> str:
        request = _with_one_shot_directive(request)
        with neutral_textgen_cwd() as cwd:
            output_path = cwd / "last-message.txt"
            await _run_cli_text_generation_command(
                "Codex",
                self.build_command(request, output_path=output_path),
                neutral_cwd=cwd,
                timeout_seconds=self._timeout_seconds,
                env_overrides=self._env,
                stdin_input=_compose_prompt(request),
            )
            return output_path.read_text(encoding="utf-8").strip()


def _droid_isolated_env(base_env: Mapping[str, str], temp_home: Path) -> dict[str, str]:
    """Build a Droid environment rooted entirely in temp state."""
    xdg_config_home = temp_home / ".config"
    xdg_data_home = temp_home / ".local" / "share"
    xdg_state_home = temp_home / ".local" / "state"
    xdg_cache_home = temp_home / ".cache"
    for path in (xdg_config_home, xdg_data_home, xdg_state_home, xdg_cache_home):
        path.mkdir(parents=True, exist_ok=True)

    env = dict(base_env)
    env.update(
        {
            "HOME": str(temp_home),
            "XDG_CONFIG_HOME": str(xdg_config_home),
            "XDG_DATA_HOME": str(xdg_data_home),
            "XDG_STATE_HOME": str(xdg_state_home),
            "XDG_CACHE_HOME": str(xdg_cache_home),
            "GOBBY_HOOKS_DISABLED": "1",
        }
    )
    return env


def _seed_droid_factory_state(base_env: Mapping[str, str], temp_home: Path) -> None:
    """Copy only auth/config support from the real Factory home into temp state."""
    if base_env.get("FACTORY_API_KEY"):
        return

    original_home = base_env.get("HOME")
    if not original_home:
        return

    source_factory = Path(original_home) / ".factory"
    if not source_factory.is_dir():
        return

    target_factory = temp_home / ".factory"
    for source_path in source_factory.rglob("*"):
        if source_path.is_symlink():
            continue
        relative_path = source_path.relative_to(source_factory)
        if not _should_seed_droid_factory_path(relative_path):
            continue

        target_path = target_factory / relative_path
        if source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
        elif source_path.is_file():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)


def _should_seed_droid_factory_path(relative_path: Path) -> bool:
    parts = relative_path.parts
    if not parts:
        return False
    if any(part in _DROID_FACTORY_EXCLUDED_NAMES for part in parts):
        return False
    if parts[:2] == ("cache", "search"):
        return False

    top_level = parts[0]
    if top_level in _DROID_FACTORY_ALLOWED_TOP_LEVEL_DIRS:
        return True
    if top_level == "cache" and len(parts) > 1:
        return parts[1] in _DROID_FACTORY_ALLOWED_CACHE_DIRS
    if top_level == "plugins":
        return (
            parts in _DROID_FACTORY_ALLOWED_PLUGIN_FILES
            or parts[:2] in _DROID_FACTORY_ALLOWED_PLUGIN_DIRS
        )
    if len(parts) == 1:
        normalized_name = top_level.lower().replace("-", "_")
        return any(keyword in normalized_name for keyword in _DROID_FACTORY_ALLOWED_FILE_KEYWORDS)
    return False


def _is_droid_auth_error(message: str) -> bool:
    normalized_message = message.lower().replace("-", " ")
    return any(
        marker in normalized_message
        for marker in (
            "api key",
            "auth",
            "credential",
            "forbidden",
            "log in",
            "login",
            "sign in",
            "token",
            "unauthorized",
            "401",
            "403",
        )
    )


class DroidCLITextGenerateAdapter:
    """One-shot text_generate adapter for Droid's noninteractive exec transport."""

    def __init__(
        self,
        *,
        command_path: str | None = None,
        timeout_seconds: float = 600.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._command_path = command_path
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})

    def build_command(self, request: TextGenerationRequest) -> list[str]:
        """Build the Droid exec command for a request."""
        path = self._command_path or shutil.which("droid")
        if not path:
            raise FileNotFoundError("Droid CLI not found in PATH")

        command = [path, "exec", "--output-format", "text"]
        if request.model:
            command.extend(["--model", request.model])
        _extend_reasoning_args(command, "droid", request.reasoning_effort)
        command.append(_compose_prompt(request))
        return command

    async def generate(self, request: TextGenerationRequest) -> str:
        command = self.build_command(request)
        env = os.environ.copy()
        env.update(self._env)

        with neutral_textgen_cwd() as cwd:
            # Droid home/state lives under the same neutral root as the process cwd,
            # so both share one lifetime instead of two unrelated temp dirs.
            temp_home = cwd / "home"
            temp_home.mkdir(parents=True, exist_ok=True)
            _seed_droid_factory_state(env, temp_home)
            isolated_env = _droid_isolated_env(env, temp_home)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=isolated_env,
                start_new_session=True,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self._timeout_seconds,
                )
            except TimeoutError as exc:
                await _cleanup_cli_process("Droid", process, reason="timeout")
                raise RuntimeError(
                    f"Droid exec timed out after {self._timeout_seconds:g}s: {shlex.join(command)}"
                ) from exc
            except asyncio.CancelledError:
                await _cleanup_cli_process("Droid", process, reason="cancellation")
                raise
            returncode = process.returncode
            if returncode:
                message = _decode(stderr).strip() or _decode(stdout).strip()
                if not env.get("FACTORY_API_KEY") and _is_droid_auth_error(message):
                    message = f"{message} {_DROID_AUTH_ERROR_HINT}"
                raise RuntimeError(f"Droid exec failed with exit code {returncode}: {message}")
            return _decode(stdout).strip()


def _claude_text_generate_adapter(config: DaemonConfig) -> TextGenerateAdapter:
    return ClaudeTextGenerateAdapter(config)


def _local_text_generate_adapter(config: DaemonConfig, endpoint_name: str) -> TextGenerateAdapter:
    return LocalTextGenerateAdapter(config, endpoint_name)
