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

    def append_blocks(self, blocks: list[dict[str, Any]] | None) -> None:
        if not blocks:
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                content = block.get("content")
                if isinstance(content, str):
                    self.append_text(content)
                continue
            self.blocks.append(dict(block))

    def append_tool_call(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        server_name: str,
        arguments: dict[str, Any],
        status: str | None = None,
        tool_kind: str | None = None,
        locations: list[dict[str, Any]] | None = None,
        content_blocks: list[dict[str, Any]] | None = None,
        raw_output: Any = None,
    ) -> None:
        tool_call: dict[str, Any] = {
            "id": tool_call_id,
            "tool_name": tool_name,
            "server_name": server_name,
            "tool_type": _classify_tool(tool_name),
            "status": status or "calling",
            "arguments": arguments,
        }
        if tool_kind:
            tool_call["tool_kind"] = tool_kind
        if locations:
            tool_call["locations"] = locations
        if content_blocks:
            tool_call["content_blocks"] = content_blocks
        if raw_output is not None:
            tool_call["raw_output"] = raw_output

        existing = self._find_tool_call(tool_call_id)
        if existing is not None:
            content_blocks_update = tool_call.pop("content_blocks", None)
            existing.update(tool_call)
            if isinstance(content_blocks_update, list):
                self._append_tool_content_blocks(existing, content_blocks_update)
            return

        self.blocks.append({"type": "tool_chain", "tool_calls": [tool_call]})

    def complete_tool_call(
        self,
        *,
        tool_call_id: str,
        success: bool,
        result: Any = None,
        error: str | None = None,
        locations: list[dict[str, Any]] | None = None,
        content_blocks: list[dict[str, Any]] | None = None,
        raw_output: Any = None,
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
                if locations:
                    tool_call["locations"] = locations
                if content_blocks:
                    self._append_tool_content_blocks(tool_call, content_blocks)
                if raw_output is not None:
                    tool_call["raw_output"] = raw_output
                return

    def _find_tool_call(self, tool_call_id: str) -> dict[str, Any] | None:
        for block in self.blocks:
            if block.get("type") != "tool_chain":
                continue
            for tool_call in block.get("tool_calls", []):
                if isinstance(tool_call, dict) and tool_call.get("id") == tool_call_id:
                    return tool_call
        return None

    @staticmethod
    def _append_tool_content_blocks(
        tool_call: dict[str, Any], content_blocks: list[dict[str, Any]]
    ) -> None:
        existing = tool_call.get("content_blocks")
        if isinstance(existing, list):
            tool_call["content_blocks"] = [*existing, *content_blocks]
            return
        tool_call["content_blocks"] = content_blocks
