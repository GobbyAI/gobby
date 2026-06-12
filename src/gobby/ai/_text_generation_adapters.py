"""Concrete daemon text generation adapters."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.ai._text_generation_contracts import (
    ACPClientFactory,
    CodexAppServerClientFactory,
    CodexAppServerClientLike,
    CodexAppServerClientProvider,
    TextGenerateAdapter,
    TextGenerationRequest,
)
from gobby.ai._text_generation_helpers import (
    _codex_completed_item_type,
    _codex_event_text,
    _collect_acp_text,
    _compose_prompt,
    _decode,
    _deny_one_shot_tool_use,
    _raise_for_codex_error_event,
    _with_one_shot_directive,
)
from gobby.config.app import DaemonConfig

if TYPE_CHECKING:
    from gobby.llm.base import LLMTextResult

logger = logging.getLogger("gobby.ai.text_generation")


_GEMINI_DENY_ALL_POLICY = """[[rule]]
toolName = "*"
decision = "deny"
priority = 999
denyMessage = "Tool use is disabled for one-shot text generation."
"""


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
            caller=request.caller,
        )

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        return await self._provider.generate_json(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
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
            caller=request.caller,
        )

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        return await self._provider.generate_json(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
            caller=request.caller,
        )


class ACPTextGenerateAdapter:
    """One-shot text_generate adapter for ACP-backed CLIs."""

    def __init__(self, client_factory: ACPClientFactory) -> None:
        self._client_factory = client_factory

    async def generate(self, request: TextGenerationRequest) -> str:
        request = _with_one_shot_directive(request)
        client = self._client_factory()
        await client.start(
            auto_session=True,
            cwd=request.cwd,
            model=request.model,
        )
        try:
            prompt = _compose_prompt(request)
            return await _collect_acp_text(
                client.send(
                    prompt,
                    model=request.model,
                    pre_tool_callback=_deny_one_shot_tool_use,
                )
            )
        finally:
            await client.stop()


async def _run_cli_text_generation_command(
    provider_name: str,
    command: Sequence[str],
    *,
    cwd: str | None,
    timeout_seconds: float,
    env_overrides: Mapping[str, str],
) -> str:
    env = os.environ.copy()
    env.update(env_overrides)
    env["GOBBY_HOOKS_DISABLED"] = "1"
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2.0)
        raise RuntimeError(
            f"{provider_name} CLI timed out after {timeout_seconds:g}s: {shlex.join(command)}"
        ) from exc

    returncode = process.returncode
    if returncode:
        message = _decode(stderr).strip() or _decode(stdout).strip()
        raise RuntimeError(f"{provider_name} CLI failed with exit code {returncode}: {message}")
    return _decode(stdout).strip()


class _GeminiCLITextGenerateAdapter:
    """One-shot text_generate adapter for Gemini headless CLI mode."""

    def __init__(
        self,
        *,
        command_path: str | None = None,
        timeout_seconds: float = 600.0,
        env: Mapping[str, str] | None = None,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._command_path = command_path
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})
        self._session_id_factory = session_id_factory or (lambda: str(uuid.uuid4()))

    def _resolve_command_path(self) -> str:
        path = self._command_path or shutil.which("gemini")
        if not path:
            raise FileNotFoundError("Gemini CLI not found in PATH")
        return path

    def build_command(
        self, request: TextGenerationRequest, *, session_id: str, policy_path: Path
    ) -> list[str]:
        path = self._resolve_command_path()
        command = [
            path,
            "--output-format",
            "text",
            "--session-id",
            session_id,
            "--admin-policy",
            str(policy_path),
            "--approval-mode",
            "plan",
        ]
        if request.model:
            command.extend(["--model", request.model])
        command.extend(["--prompt", _compose_prompt(request)])
        return command

    def build_cleanup_command(self, session_id: str) -> list[str]:
        return [self._resolve_command_path(), "--delete-session", session_id]

    async def generate(self, request: TextGenerationRequest) -> str:
        request = _with_one_shot_directive(request)
        session_id = self._session_id_factory()
        if not session_id:
            raise RuntimeError("Gemini CLI session ID factory returned an empty session ID")

        with tempfile.TemporaryDirectory(prefix="gobby-gemini-textgen-") as temp_dir:
            policy_path = Path(temp_dir) / "deny-all-tools.toml"
            policy_path.write_text(_GEMINI_DENY_ALL_POLICY, encoding="utf-8")
            command = self.build_command(request, session_id=session_id, policy_path=policy_path)
            cleanup_command = self.build_cleanup_command(session_id)
            try:
                return await _run_cli_text_generation_command(
                    "Gemini",
                    command,
                    cwd=request.cwd,
                    timeout_seconds=self._timeout_seconds,
                    env_overrides=self._env,
                )
            finally:
                await _run_cli_text_generation_command(
                    "Gemini session cleanup",
                    cleanup_command,
                    cwd=request.cwd,
                    timeout_seconds=self._timeout_seconds,
                    env_overrides=self._env,
                )


class _QwenCLITextGenerateAdapter:
    """One-shot text_generate adapter for Qwen positional headless CLI mode."""

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
        path = self._command_path or shutil.which("qwen")
        if not path:
            raise FileNotFoundError("Qwen CLI not found in PATH")

        command = [
            path,
            "--chat-recording=false",
            "--max-tool-calls",
            "0",
            "--max-session-turns",
            "1",
            "--output-format",
            "text",
        ]
        if request.model:
            command.extend(["--model", request.model])
        command.append(_compose_prompt(request))
        return command

    async def generate(self, request: TextGenerationRequest) -> str:
        request = _with_one_shot_directive(request)
        return await _run_cli_text_generation_command(
            "Qwen",
            self.build_command(request),
            cwd=request.cwd,
            timeout_seconds=self._timeout_seconds,
            env_overrides=self._env,
        )


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
        command.extend(["--single", _compose_prompt(request)])
        return command

    async def generate(self, request: TextGenerationRequest) -> str:
        request = _with_one_shot_directive(request)
        with tempfile.TemporaryDirectory(prefix="gobby-grok-textgen-") as temp_dir:
            leader_socket = Path(temp_dir) / "leader.sock"
            return await _run_cli_text_generation_command(
                "Grok",
                self.build_command(request, leader_socket=leader_socket),
                cwd=request.cwd,
                timeout_seconds=self._timeout_seconds,
                env_overrides=self._env,
            )


class CodexAppServerTextGenerateAdapter:
    """One-shot text_generate adapter backed by Codex app-server."""

    def __init__(
        self,
        client_factory: CodexAppServerClientFactory | None = None,
        *,
        shared_client_provider: CodexAppServerClientProvider | None = None,
        timeout_seconds: float = 600.0,
    ) -> None:
        self._client_factory = client_factory or _codex_app_server_client
        self._shared_client_provider = shared_client_provider
        self._timeout_seconds = timeout_seconds

    async def generate(self, request: TextGenerationRequest) -> str:
        shared_client = self._connected_shared_client()
        if shared_client is not None:
            try:
                return await self._generate_with_deadline(
                    shared_client,
                    request,
                    start_client=False,
                    archive_thread=True,
                )
            except TimeoutError as exc:
                if shared_client.is_connected:
                    raise RuntimeError(
                        "Codex app-server text generation timed out after "
                        f"{self._timeout_seconds:g}s"
                    ) from exc
                logger.info("Borrowed Codex app-server disconnected during generation")
            except Exception:
                if shared_client.is_connected:
                    raise
                logger.info("Borrowed Codex app-server disconnected during generation")

        return await self._generate_with_owned_client(request)

    async def _generate_with_owned_client(self, request: TextGenerationRequest) -> str:
        client = self._client_factory()
        try:
            return await self._generate_with_deadline(
                client,
                request,
                start_client=True,
                archive_thread=False,
            )
        except TimeoutError as exc:
            raise RuntimeError(
                f"Codex app-server text generation timed out after {self._timeout_seconds:g}s"
            ) from exc
        finally:
            await client.stop()

    async def _generate_with_deadline(
        self,
        client: CodexAppServerClientLike,
        request: TextGenerationRequest,
        *,
        start_client: bool,
        archive_thread: bool,
    ) -> str:
        return await asyncio.wait_for(
            self._generate_with_client(
                client,
                request,
                start_client=start_client,
                archive_thread=archive_thread,
            ),
            timeout=self._timeout_seconds,
        )

    async def _generate_with_client(
        self,
        client: CodexAppServerClientLike,
        request: TextGenerationRequest,
        *,
        start_client: bool,
        archive_thread: bool,
    ) -> str:
        request = _with_one_shot_directive(request)
        if start_client:
            await client.start()
        thread_id: str | None = None
        try:
            thread = await client.start_thread(
                cwd=request.cwd,
                model=request.model,
                approval_policy="never",
                sandbox="readOnly",
                ephemeral=True,
            )
            thread_id = thread.id
            chunks: list[str] = []
            fallback_chunks: list[str] = []
            async for event in client.run_turn(
                thread.id,
                request.prompt,
                context_prefix=request.system_prompt,
            ):
                _raise_for_codex_error_event(event)
                event_type = event.get("type")
                text = _codex_event_text(event)
                if not text:
                    continue
                if event_type in {"agent/messageDelta", "item/agentMessage/delta"}:
                    chunks.append(text)
                elif (
                    event_type == "item/completed"
                    and _codex_completed_item_type(event) == "agentMessage"
                ):
                    fallback_chunks.append(text)
            output = "".join(chunks or fallback_chunks).strip()
            if not output:
                raise RuntimeError("Codex text generation returned no output")
            return output
        finally:
            if archive_thread and thread_id is not None:
                await self._archive_borrowed_thread(client, thread_id)

    def _connected_shared_client(self) -> CodexAppServerClientLike | None:
        if self._shared_client_provider is None:
            return None
        try:
            client = self._shared_client_provider()
        except Exception:
            logger.debug("Shared Codex app-server provider failed", exc_info=True)
            return None
        if client is None:
            return None
        try:
            return client if client.is_connected else None
        except Exception:
            logger.debug("Shared Codex app-server connection check failed", exc_info=True)
            return None

    async def _archive_borrowed_thread(
        self, client: CodexAppServerClientLike, thread_id: str
    ) -> None:
        with contextlib.suppress(Exception):
            await client.archive_thread(thread_id)


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
        command.append(_compose_prompt(request))
        return command

    async def generate(self, request: TextGenerationRequest) -> str:
        command = self.build_command(request)
        env = os.environ.copy()
        env.update(self._env)
        env["GOBBY_HOOKS_DISABLED"] = "1"
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=request.cwd,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2.0)
            raise RuntimeError(
                f"Droid exec timed out after {self._timeout_seconds:g}s: {shlex.join(command)}"
            ) from exc
        returncode = process.returncode
        if returncode:
            message = _decode(stderr).strip() or _decode(stdout).strip()
            raise RuntimeError(f"Droid exec failed with exit code {returncode}: {message}")
        return _decode(stdout).strip()


def _claude_text_generate_adapter(config: DaemonConfig) -> TextGenerateAdapter:
    return ClaudeTextGenerateAdapter(config)


def _local_text_generate_adapter(config: DaemonConfig, endpoint_name: str) -> TextGenerateAdapter:
    return LocalTextGenerateAdapter(config, endpoint_name)


def _codex_app_server_client() -> CodexAppServerClientLike:
    from gobby.adapters.codex_impl.client import CodexAppServerClient

    return CodexAppServerClient()
