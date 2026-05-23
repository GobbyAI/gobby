"""Grok concrete ACP client."""

from __future__ import annotations

from typing import Any, ClassVar

from gobby.adapters.acp_client import ACP_PROMPT_TIMEOUT_ENV_GROK, ACPClient, StreamEvent


class GrokACPClient(ACPClient):
    """ACP client for Grok's ``agent stdio`` transport."""

    cli_name: ClassVar[str] = "grok"
    display_name: ClassVar[str] = "Grok"
    prompt_timeout_env: ClassVar[str] = ACP_PROMPT_TIMEOUT_ENV_GROK
    supports_cached_auth: ClassVar[bool] = True

    def _build_launch_command(
        self,
        path: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> list[str]:
        cmd = [path, "agent", "--no-leader", "--always-approve"]
        if model:
            cmd.extend(["--model", model])
        if reasoning_effort:
            cmd.extend(["--reasoning-effort", reasoning_effort])
        if self._extra_args:
            cmd.extend(self._extra_args)
        cmd.append("stdio")
        return cmd

    @classmethod
    def _normalize_notification(cls, raw: dict[str, Any]) -> StreamEvent:
        method = raw.get("method", "")
        params = raw.get("params", {})
        if method not in {"session/update", "_x.ai/session_notification"}:
            return super()._normalize_notification(raw)

        if not isinstance(params, dict):
            return StreamEvent(event_type=method or "unknown", data=raw)
        update = params.get("update")
        if not isinstance(update, dict):
            return StreamEvent(event_type=method, data=params)

        update_type = str(update.get("sessionUpdate") or "")
        content = update.get("content")
        text = cls._extract_text_content(content)

        if update_type == "agent_message_chunk":
            return StreamEvent(
                event_type="content_delta",
                data={
                    "content": text,
                    "role": "assistant",
                    "message_id": update.get("messageId"),
                },
            )
        if update_type == "agent_thought_chunk":
            return StreamEvent(
                event_type="thinking_delta",
                data={"content": text, "message_id": update.get("messageId")},
            )
        if update_type == "user_message_chunk":
            return StreamEvent(
                event_type="message",
                data={
                    "role": "user",
                    "content": text,
                    "message_id": update.get("messageId"),
                },
            )
        if update_type == "tool_call":
            return StreamEvent(
                event_type="tool_call",
                data={
                    "call_id": update.get("toolCallId"),
                    "tool_name": update.get("title") or update.get("name"),
                    "tool_input": update.get("rawInput") or update.get("input") or {},
                },
            )
        if update_type == "tool_call_update":
            result = _extract_tool_update_result(update)
            if result is not None:
                return StreamEvent(
                    event_type="tool_result",
                    data={
                        "call_id": update.get("toolCallId"),
                        "success": not bool(result.get("error")),
                        "result": result,
                        "error": result.get("error"),
                    },
                )
            return StreamEvent(event_type="tool_update", data=update)
        if update_type == "tool_call_delta_chunk":
            return StreamEvent(event_type="tool_call_delta", data=update)

        return StreamEvent(event_type=update_type or method, data=update)


def _extract_tool_update_result(update: dict[str, Any]) -> dict[str, Any] | None:
    content = update.get("content")
    if not isinstance(content, list):
        return None
    text_parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        nested = item.get("content")
        if isinstance(nested, dict):
            text = nested.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
    if not text_parts:
        return None
    return {"output": "\n".join(text_parts), "raw": update}


__all__ = ["GrokACPClient"]
