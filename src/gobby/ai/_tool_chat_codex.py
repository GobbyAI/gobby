"""Codex app-server tool-loop enforcement for ``tool_chat``."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol, cast

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.ai._tool_chat_contracts import (
    LIMIT_STOP_REASONS,
    MAX_TOOL_CALLS_STOP_REASON,
    MAX_TURNS_STOP_REASON,
    ToolChatRequest,
    ToolChatResult,
)
from gobby.ai._tool_chat_tools import (
    ToolPolicyError,
    ToolRuntime,
    tool_result_is_error,
    validate_policy,
)
from gobby.ai.codex_endpoint import (
    codex_endpoint_app_server_env,
    codex_endpoint_config_overrides,
    codex_event_text,
)
from gobby.ai.endpoints import resolve_generation_endpoint
from gobby.ai.registry import (
    AICapability,
    CapabilityBinding,
    CapabilityUnavailableError,
)
from gobby.config.app import DaemonConfig

logger = logging.getLogger(__name__)

_DYNAMIC_TOOL_METHOD = "item/tool/call"
_RAW_RESPONSE_COMPLETED = "rawResponse/completed"
_NATIVE_TOOL_DISABLE_OVERRIDES = (
    "mcp_servers={}",
    "features.shell_tool=false",
    "features.unified_exec=false",
    "features.apply_patch_freeform=false",
    "features.web_search_cached=false",
    "features.web_search_request=false",
    "features.standalone_web_search=false",
    "features.image_generation=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.browser_use_full_cdp_access=false",
    "features.in_app_browser=false",
    "features.computer_use=false",
    "features.js_repl=false",
    "features.code_mode=false",
    "features.code_mode_host=false",
    "features.artifact=false",
    "features.apps=false",
    "features.enable_mcp_apps=false",
    "features.plugins=false",
    "features.skill_search=false",
    "features.skill_mcp_dependency_install=false",
    "features.tool_suggest=false",
    "features.goals=false",
    "features.request_permissions_tool=false",
    "features.multi_agent=false",
    "features.multi_agent_v2=false",
    "tools.web_search=false",
)

DynamicToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
NotificationHandler = Callable[[str, dict[str, Any]], None]


class CodexThreadRef(Protocol):
    id: str


class CodexToolChatClient(Protocol):
    def register_request_handler(
        self,
        method: str,
        handler: DynamicToolHandler,
    ) -> None: ...

    def remove_request_handler(self, method: str) -> None: ...

    def add_notification_handler(
        self,
        method: str,
        handler: NotificationHandler,
    ) -> None: ...

    def remove_notification_handler(
        self,
        method: str,
        handler: NotificationHandler,
    ) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def start_thread(
        self,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        ephemeral: bool = False,
        dynamic_tools: list[dict[str, Any]] | None = None,
        experimental_raw_events: bool = False,
    ) -> CodexThreadRef: ...

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None: ...

    def run_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        **config_overrides: Any,
    ) -> AsyncIterator[dict[str, Any]]: ...


CodexClientFactory = Callable[..., CodexToolChatClient]


def _dynamic_tool_specs(runtime: ToolRuntime) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": name,
            "description": runtime.description_for(name),
            "inputSchema": runtime.input_schema_for(name),
        }
        for name in runtime.tool_names()
    ]


def _is_tool_error(text: str) -> bool:
    return tool_result_is_error(text)


def _capability_error(
    binding: CapabilityBinding,
    model: str | None,
    detail: str,
) -> CapabilityUnavailableError:
    return CapabilityUnavailableError(
        AICapability.TOOL_CHAT,
        provider=binding.provider,
        model=model,
        reason=f"Codex app-server tool-loop protocol unavailable: {detail}",
    )


def _looks_like_protocol_incompatibility(error: Exception) -> bool:
    detail = str(error).lower()
    markers = (
        "dynamictools",
        "experimentalrawevents",
        "experimentalapi",
        "item/tool/call",
        "unknown field",
        "invalid params",
        "method not found",
    )
    return any(marker in detail for marker in markers)


class CodexSpawnToolChatAdapter:
    """Enforce the shared loop contract through Codex app-server dynamic tools."""

    def __init__(
        self,
        *,
        command_path: str | None = None,
        config: DaemonConfig | None = None,
        client_factory: CodexClientFactory = CodexAppServerClient,
    ) -> None:
        self._command_path = command_path
        self._config = config
        self._client_factory = client_factory

    def _resolve_command_path(self) -> str:
        path = self._command_path or shutil.which("codex")
        if not path:
            raise FileNotFoundError("Codex CLI not found in PATH")
        return path

    def _client_options(
        self,
        binding: CapabilityBinding,
        *,
        model: str | None,
    ) -> dict[str, Any]:
        config_overrides: tuple[str, ...] = _NATIVE_TOOL_DISABLE_OVERRIDES
        env_overrides: dict[str, str] = {}
        endpoint_name = binding.metadata.get("endpoint")
        if binding.metadata.get("wire_api") == "responses":
            if self._config is None or not isinstance(endpoint_name, str):
                raise ValueError("Responses tool_chat binding is missing endpoint configuration")
            endpoint = resolve_generation_endpoint(self._config, endpoint_name)
            config_overrides = (
                *codex_endpoint_config_overrides(endpoint_name, endpoint, model=model),
                *_NATIVE_TOOL_DISABLE_OVERRIDES,
            )
            env_overrides.update(codex_endpoint_app_server_env(endpoint_name, endpoint))
        return {
            "codex_command": self._resolve_command_path(),
            "config_overrides": config_overrides,
            "env_overrides": env_overrides,
            "experimental_api": True,
        }

    async def chat(
        self,
        request: ToolChatRequest,
        binding: CapabilityBinding,
    ) -> ToolChatResult:
        validate_policy(request.tool_policy)
        limits = request.effective_limits
        runtime = ToolRuntime(
            request.tool_policy,
            project_path=request.project_path,
            limits=limits,
            builtins=request.builtins,
            subprocess_env=request.managed_subprocess_env,
            managed_execution_id=request.managed_execution_id,
        )
        model = request.model or next(iter(binding.models), None)
        client = self._client_factory(**self._client_options(binding, model=model))
        turns = 0
        text = ""
        stop_reason = "completed"
        thread_id: str | None = None
        response_metadata: dict[str, object] = {}
        request_parameters = cast(dict[str, Any], dict(request.request_parameters))

        def record_raw_response(
            _method: str,
            _params: dict[str, Any],
        ) -> None:
            nonlocal stop_reason, turns
            response_metadata.update(_speed_response_metadata(_params))
            turns += 1
            if limits.max_turns is None or turns < limits.max_turns or stop_reason != "completed":
                return
            stop_reason = MAX_TURNS_STOP_REASON
            current_turn_id = _params.get("turnId")
            if thread_id and isinstance(current_turn_id, str):
                asyncio.create_task(
                    client.interrupt_turn(thread_id, current_turn_id),
                    name=f"tool-chat-max-turns:{current_turn_id}",
                )

        async def handle_dynamic_tool(params: dict[str, Any]) -> dict[str, Any]:
            nonlocal stop_reason
            current_turn_id = params.get("turnId")
            if limits.max_turns is not None and turns >= limits.max_turns:
                stop_reason = MAX_TURNS_STOP_REASON
                if thread_id and isinstance(current_turn_id, str):
                    await client.interrupt_turn(thread_id, current_turn_id)
                result_text = "[error: max_turns exhausted before tool execution]"
                return {
                    "contentItems": [{"type": "inputText", "text": result_text}],
                    "success": False,
                }
            if runtime.budget_exhausted:
                stop_reason = MAX_TOOL_CALLS_STOP_REASON
                if thread_id and isinstance(current_turn_id, str):
                    await client.interrupt_turn(thread_id, current_turn_id)
                result_text = "[error: max_tool_calls exhausted before tool execution]"
                return {
                    "contentItems": [{"type": "inputText", "text": result_text}],
                    "success": False,
                }
            tool_name = params.get("tool")
            arguments = params.get("arguments")
            if not isinstance(tool_name, str):
                result_text = "[error: dynamic tool request omitted tool name]"
                return {
                    "contentItems": [{"type": "inputText", "text": result_text}],
                    "success": False,
                }
            try:
                result_text = await runtime.execute(tool_name, arguments)
            except ToolPolicyError as exc:
                result_text = f"[error: {exc}]"
            return {
                "contentItems": [{"type": "inputText", "text": result_text}],
                "success": not _is_tool_error(result_text),
            }

        client_started = False
        request_handler_registered = False
        notification_handler_registered = False

        async def cleanup_client(*, suppress_errors: bool) -> None:
            cleanup_error: BaseException | None = None
            if notification_handler_registered:
                try:
                    client.remove_notification_handler(
                        _RAW_RESPONSE_COMPLETED,
                        record_raw_response,
                    )
                except BaseException as exc:
                    cleanup_error = exc
            if request_handler_registered:
                try:
                    client.remove_request_handler(_DYNAMIC_TOOL_METHOD)
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            if client_started:
                try:
                    await client.stop()
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            if cleanup_error is None:
                return
            if suppress_errors:
                logger.warning(
                    "Codex tool_chat cleanup failed while preserving the active exception",
                    exc_info=(
                        type(cleanup_error),
                        cleanup_error,
                        cleanup_error.__traceback__,
                    ),
                )
                return
            raise cleanup_error

        try:
            try:
                await client.start()
                client_started = True
                client.register_request_handler(_DYNAMIC_TOOL_METHOD, handle_dynamic_tool)
                request_handler_registered = True
                client.add_notification_handler(_RAW_RESPONSE_COMPLETED, record_raw_response)
                notification_handler_registered = True
                thread = await client.start_thread(
                    cwd=request.project_path,
                    model=model,
                    approval_policy="never",
                    sandbox="readOnly",
                    ephemeral=True,
                    dynamic_tools=_dynamic_tool_specs(runtime),
                    experimental_raw_events=True,
                )
            except Exception as exc:
                if _looks_like_protocol_incompatibility(exc):
                    raise _capability_error(binding, model, str(exc)) from exc
                raise
            thread_id = thread.id
            async for event in client.run_turn(
                thread.id,
                request.prompt,
                context_prefix=request.system_prompt,
                effort=request.reasoning_effort if request.reasoning_effort != "auto" else None,
                **request_parameters,
            ):
                event_type = event.get("type")
                if event_type == "item/completed":
                    candidate = codex_event_text(event)
                    if candidate:
                        text = candidate
        except BaseException:
            await cleanup_client(suppress_errors=True)
            raise
        else:
            await cleanup_client(suppress_errors=False)

        if turns == 0:
            raise _capability_error(
                binding,
                model,
                "missing rawResponse/completed events",
            )
        if stop_reason == "completed" and not text:
            raise RuntimeError(
                "Codex tool_chat produced no final message "
                f"(model={model}, tool_use_count={runtime.calls_used})"
            )
        return ToolChatResult(
            text=text if stop_reason == "completed" else "",
            provider=binding.provider,
            model=model,
            tool_use_count=runtime.calls_used,
            turns=turns,
            tools={
                name: sum(1 for item in runtime.invocation_log if item.get("tool_name") == name)
                for name in runtime.tool_names()
                if any(item.get("tool_name") == name for item in runtime.invocation_log)
            },
            applied_reasoning_effort=(
                request.reasoning_effort if request.reasoning_effort != "auto" else None
            ),
            stop_reason=stop_reason,
            trace=tuple(runtime.invocation_log),
            calls_used=runtime.calls_used,
            budget_exhausted=stop_reason in LIMIT_STOP_REASONS,
            trace_available=True,
            response_metadata=response_metadata,
        )


def _speed_response_metadata(params: dict[str, Any]) -> dict[str, object]:
    containers = [params]
    for key in ("response", "rawResponse", "raw_response"):
        nested = params.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    metadata: dict[str, object] = {}
    for container in containers:
        for key in ("serviceTier", "service_tier", "tier"):
            value = container.get(key)
            if isinstance(value, str):
                metadata[key] = value
    return metadata
