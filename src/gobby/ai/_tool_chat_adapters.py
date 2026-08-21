"""``tool_chat`` adapter implementations, one per :class:`AIAdapterStyle` family.

Adapters receive a :class:`~gobby.ai._tool_chat_contracts.ToolChatRequest` and the
selected :class:`~gobby.ai.registry.CapabilityBinding`, construct their concrete
provider from the binding (provider names are allowed in this adapter layer), and
run the agent under the request's tool policy and directive.

Family A (this module): ``openai_compatible`` / ``local`` inference servers
(lm-studio, ollama) that do not run their own agent loop — the daemon drives an
OpenAI tool-calling loop over the caller's tools, executing each call in
``cwd=project_path`` via :class:`~gobby.ai._tool_chat_tools.ToolRuntime`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from gobby.ai._tool_chat_contracts import (
    MAX_TOOL_CALLS_STOP_REASON,
    MAX_TURNS_STOP_REASON,
    ToolChatRequest,
    ToolChatResult,
)
from gobby.ai._tool_chat_tools import ToolPolicyError, ToolRuntime, tool_result_is_error
from gobby.ai.registry import CapabilityBinding
from gobby.llm.claude_errors import ClaudeSDKMaxTurns

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 8000

# Given the selected binding, return an AsyncOpenAI-compatible client whose
# ``chat.completions.create`` accepts ``tools``/``tool_choice``.
OpenAIClientFactory = Callable[[CapabilityBinding], Any]
# Given the selected binding and the requested model (None = binding default),
# return the model id to put on the wire (vllm ``auto`` -> the served id).
OpenAIModelResolver = Callable[[CapabilityBinding, str | None], Awaitable[str]]


class OpenAICompatibleToolChatAdapter:
    """Family A adapter: a daemon-run OpenAI tool-calling loop.

    The model is driven over the caller's tool policy until it stops requesting
    tools or the loop bounds (``ToolLoopLimits``) are hit. Tool execution is the
    caller's read/whitelisted CLI surface, run in ``cwd=project_path``.
    """

    def __init__(
        self,
        client_factory: OpenAIClientFactory,
        *,
        model_resolver: OpenAIModelResolver | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._model_resolver = model_resolver

    async def _wire_model(self, request: ToolChatRequest, binding: CapabilityBinding) -> str:
        if self._model_resolver is not None:
            return await self._model_resolver(binding, request.model)
        model = request.model or next(iter(binding.models), None)
        if model is None:
            raise ValueError("openai_compatible tool_chat binding has no model to call")
        return model

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        limits = request.effective_limits
        runtime = ToolRuntime(
            request.tool_policy,
            project_path=request.project_path,
            limits=limits,
            builtins=request.builtins,
            subprocess_env=request.managed_subprocess_env,
            managed_execution_id=request.managed_execution_id,
        )
        model = await self._wire_model(request, binding)

        client = self._client_factory(binding)
        tools = runtime.openai_schemas()
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": request.system_prompt or "You are a helpful assistant.",
            },
            {"role": "user", "content": request.prompt},
        ]
        usage_total: dict[str, int] = {}
        tool_breakdown: dict[str, int] = {}
        turns = 0
        content = ""
        stop_reason = MAX_TURNS_STOP_REASON
        response_metadata: dict[str, object] = {}

        while limits.max_turns is None or turns < limits.max_turns:
            turns += 1
            response = await client.chat.completions.create(
                **_completion_kwargs(model, messages, tools, request)
            )
            message = response.choices[0].message
            service_tier = getattr(response, "service_tier", None)
            if isinstance(service_tier, str):
                response_metadata["serviceTier"] = service_tier
            _accumulate_usage(usage_total, getattr(response, "usage", None))

            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                content = message.content or ""
                stop_reason = "completed"
                break

            if limits.max_turns is not None and turns >= limits.max_turns:
                stop_reason = MAX_TURNS_STOP_REASON
                break

            messages.append(_assistant_message(message, tool_calls))
            hit_call_cap = False
            for call in tool_calls:
                if runtime.budget_exhausted:
                    hit_call_cap = True
                    break
                name = call.function.name
                tool_breakdown[name] = tool_breakdown.get(name, 0) + 1
                result_text = await _run_tool(runtime, call)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result_text,
                    }
                )
            if hit_call_cap:
                stop_reason = MAX_TOOL_CALLS_STOP_REASON
                break

        return ToolChatResult(
            text=content,
            provider=binding.provider,
            model=model,
            tool_use_count=runtime.calls_used,
            turns=turns,
            tools=tool_breakdown,
            usage=usage_total or None,
            applied_reasoning_effort=request.reasoning_effort,
            stop_reason=stop_reason,
            trace=tuple(runtime.invocation_log),
            calls_used=runtime.calls_used,
            budget_exhausted=runtime.budget_exhausted,
            trace_available=True,
            response_metadata=response_metadata,
        )


def _completion_kwargs(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    request: ToolChatRequest,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": request.max_tokens or _DEFAULT_MAX_TOKENS,
    }
    if request.reasoning_effort is not None:
        kwargs["reasoning_effort"] = request.reasoning_effort
    kwargs.update(request.request_parameters)
    return kwargs


def _assistant_message(message: Any, tool_calls: Any) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments or "{}",
                },
            }
            for call in tool_calls
        ],
    }


async def _run_tool(runtime: ToolRuntime, call: Any) -> str:
    raw = call.function.arguments or "{}"
    try:
        arguments = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return "[error: tool arguments were not valid JSON]"
    try:
        return await runtime.execute(call.function.name, arguments)
    except ToolPolicyError as exc:
        return f"[error: {exc}]"


def _accumulate_usage(total: dict[str, int], usage: Any) -> None:
    if usage is None:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


# ---------------------------------------------------------------------------
# Family B: external agent runs its own loop (llm_provider = Claude Agent SDK)
# ---------------------------------------------------------------------------

# Built-in agent tools denied for every tool_chat run. Mutation is permitted
# only through a caller's declared (policy-validated) MCP tools — never through
# raw shell/file tools — so a read-only policy cannot write to the repo and a
# write-capable policy still mutates only via its own tools.
_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Task",
    "Agent",
)
_REPO_MCP_SERVER_NAME = "repo"

# Given the selected binding, return a provider exposing ``generate_agentic``.
ClaudeProviderFactory = Callable[[CapabilityBinding], Any]


def _mcp_tool_name(tool_name: str) -> str:
    return f"mcp__{_REPO_MCP_SERVER_NAME}__{tool_name}"


def _make_tool_handler(
    runtime: ToolRuntime, tool_name: str
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            text = await runtime.execute(tool_name, args)
            response: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
            if tool_result_is_error(text):
                response["is_error"] = True
            return response
        except ToolPolicyError as exc:
            return {
                "content": [{"type": "text", "text": f"[error: {exc}]"}],
                "is_error": True,
            }

    return handler


def build_repo_mcp_server(runtime: ToolRuntime) -> tuple[Any, list[str]]:
    """Build an in-process SDK MCP server exposing the policy's tools.

    Returns ``(server_config, allowed_tool_names)``. Each tool delegates to the
    shared :class:`ToolRuntime`, so policy enforcement (read-only/project_path/
    caps) is identical to the Family A loop.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    sdk_tools = []
    allowed: list[str] = []
    for tool_name in runtime.tool_names():
        sdk_tools.append(
            tool(
                tool_name,
                runtime.description_for(tool_name),
                runtime.input_schema_for(tool_name),
            )(_make_tool_handler(runtime, tool_name))
        )
        allowed.append(_mcp_tool_name(tool_name))
    server = create_sdk_mcp_server(name=_REPO_MCP_SERVER_NAME, version="1.0.0", tools=sdk_tools)
    return server, allowed


class ClaudeToolChatAdapter:
    """Family B adapter for the ``llm_provider`` style (Claude Agent SDK).

    Exposes the caller's tool policy as an in-process MCP server and runs the
    Agent SDK loop with mutation/shell tools denied, so a read-only policy
    cannot write to the repo. The concrete provider is built from the binding.
    """

    def __init__(self, provider_factory: ClaudeProviderFactory) -> None:
        self._provider_factory = provider_factory

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        limits = request.effective_limits
        runtime = ToolRuntime(
            request.tool_policy,
            project_path=request.project_path,
            limits=limits,
            builtins=request.builtins,
            subprocess_env=request.managed_subprocess_env,
            managed_execution_id=request.managed_execution_id,
        )
        server, allowed_tools = build_repo_mcp_server(runtime)
        provider = self._provider_factory(binding)
        model = request.model or next(iter(binding.models), None)
        max_turns = limits.max_turns
        try:
            result = await provider.generate_agentic(
                system_prompt=request.system_prompt,
                prompt=request.prompt,
                project_path=request.project_path,
                model=model,
                max_turns=max_turns,
                reasoning_effort=request.reasoning_effort,
                allowed_tools=tuple(allowed_tools),
                disallowed_tools=_DISALLOWED_TOOLS,
                mcp_servers={_REPO_MCP_SERVER_NAME: server},
                caller=request.caller or "tool_chat-llm_provider",
            )
        except ClaudeSDKMaxTurns:
            return ToolChatResult(
                text="",
                provider=binding.provider,
                model=model,
                tool_use_count=runtime.calls_used,
                turns=max_turns,
                stop_reason=MAX_TURNS_STOP_REASON,
                trace=tuple(runtime.invocation_log),
                calls_used=runtime.calls_used,
                budget_exhausted=True,
                trace_available=True,
            )
        return ToolChatResult(
            text=result.text,
            provider=binding.provider,
            model=getattr(result, "model", None) or model,
            tool_use_count=getattr(result, "tool_use_count", 0),
            turns=getattr(result, "turns", 0),
            tools=dict(getattr(result, "tools", {}) or {}),
            usage=getattr(result, "usage", None),
            applied_reasoning_effort=getattr(result, "applied_reasoning_effort", None),
            stop_reason=MAX_TOOL_CALLS_STOP_REASON if runtime.budget_exhausted else "completed",
            trace=tuple(runtime.invocation_log),
            calls_used=runtime.calls_used,
            budget_exhausted=runtime.budget_exhausted,
            trace_available=True,
        )
