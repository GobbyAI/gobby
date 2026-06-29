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
from collections.abc import Callable
from typing import Any

from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolChatResult
from gobby.ai._tool_chat_tools import ToolPolicyError, ToolRuntime
from gobby.ai.registry import CapabilityBinding

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 8000

# Given the selected binding, return an AsyncOpenAI-compatible client whose
# ``chat.completions.create`` accepts ``tools``/``tool_choice``.
OpenAIClientFactory = Callable[[CapabilityBinding], Any]


class OpenAICompatibleToolChatAdapter:
    """Family A adapter: a daemon-run OpenAI tool-calling loop.

    The model is driven over the caller's tool policy until it stops requesting
    tools or the loop bounds (``ToolLoopLimits``) are hit. Tool execution is the
    caller's read/whitelisted CLI surface, run in ``cwd=project_path``.
    """

    def __init__(self, client_factory: OpenAIClientFactory) -> None:
        self._client_factory = client_factory

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        runtime = ToolRuntime(
            request.tool_policy,
            project_path=request.project_path,
            limits=request.limits,
        )
        model = request.model or next(iter(binding.models), None)
        if model is None:
            raise ValueError("openai_compatible tool_chat binding has no model to call")

        client = self._client_factory(binding)
        tools = runtime.openai_schemas()
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": request.system_prompt or "You are a helpful assistant.",
            },
            {"role": "user", "content": request.prompt},
        ]
        limits = request.limits

        usage_total: dict[str, int] = {}
        tool_breakdown: dict[str, int] = {}
        tool_use_count = 0
        turns = 0
        content = ""
        stop_reason = "max_turns"

        for _ in range(limits.max_turns):
            turns += 1
            response = await client.chat.completions.create(
                **_completion_kwargs(model, messages, tools, request)
            )
            message = response.choices[0].message
            _accumulate_usage(usage_total, getattr(response, "usage", None))

            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                content = message.content or ""
                stop_reason = "completed"
                break

            messages.append(_assistant_message(message, tool_calls))
            hit_call_cap = False
            for call in tool_calls:
                if tool_use_count >= limits.max_tool_calls:
                    hit_call_cap = True
                    break
                tool_use_count += 1
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
                stop_reason = "max_tool_calls"
                break

        return ToolChatResult(
            text=content,
            provider=binding.provider,
            model=model,
            tool_use_count=tool_use_count,
            turns=turns,
            tools=tool_breakdown,
            usage=usage_total or None,
            applied_reasoning_effort=request.reasoning_effort,
            stop_reason=stop_reason,
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
