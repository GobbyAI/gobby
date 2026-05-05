"""Helpers for persisting rich web chat message content blocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _classify_tool(tool_name: str) -> str:
    name = tool_name.lower()
    if name in {"bash", "sh", "terminal", "shell", "run_command", "exec_command"}:
        return "bash"
    if name in {"read", "read_file", "cat"}:
        return "read"
    if name in {"edit", "write", "multiedit", "patch", "sed"}:
        return "edit"
    if name in {"grep", "rg", "search"}:
        return "grep"
    if name in {"glob", "ls", "list_files", "find"}:
        return "glob"
    if name.startswith("mcp__"):
        return "mcp"
    return "unknown"


@dataclass
class AssistantContentBlocks:
    """Accumulate interleaved assistant text, thinking, and tool blocks."""

    blocks: list[dict[str, Any]] = field(default_factory=list)
    _text_parts: list[str] = field(default_factory=list)

    @property
    def visible_text(self) -> str:
        return "".join(self._text_parts)

    def has_content(self) -> bool:
        return bool(self.blocks or self.visible_text.strip())

    def reset(self) -> None:
        self.blocks.clear()
        self._text_parts.clear()

    def append_text(self, content: str) -> None:
        if not content:
            return
        self._text_parts.append(content)
        last = self.blocks[-1] if self.blocks else None
        if last and last.get("type") == "text":
            last["content"] = f"{last.get('content', '')}{content}"
            return
        self.blocks.append({"type": "text", "content": content})

    def append_thinking(self, content: str) -> None:
        if not content:
            return
        last = self.blocks[-1] if self.blocks else None
        if last and last.get("type") == "thinking":
            last["content"] = f"{last.get('content', '')}{content}"
            return
        self.blocks.append({"type": "thinking", "content": content})

    def append_tool_call(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        server_name: str,
        arguments: dict[str, Any],
    ) -> None:
        tool_call = {
            "id": tool_call_id,
            "tool_name": tool_name,
            "server_name": server_name,
            "tool_type": _classify_tool(tool_name),
            "status": "calling",
            "arguments": arguments,
        }
        self.blocks.append({"type": "tool_chain", "tool_calls": [tool_call]})

    def complete_tool_call(
        self,
        *,
        tool_call_id: str,
        success: bool,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        for block in self.blocks:
            if block.get("type") != "tool_chain":
                continue
            for tool_call in block.get("tool_calls", []):
                if tool_call.get("id") != tool_call_id:
                    continue
                tool_call["status"] = "completed" if success else "error"
                if success:
                    tool_call["result"] = result
                else:
                    tool_call["error"] = error
                return
