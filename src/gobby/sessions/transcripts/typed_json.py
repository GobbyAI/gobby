"""Provider-neutral typed JSON transcript parser infrastructure."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from gobby.sessions.transcripts.base import (
    BaseTranscriptParser,
    ParsedMessage,
    ParseEvent,
    RawLine,
    TokenUsage,
    _unknown_block_message,
    annotate_record_source,
)

logger = logging.getLogger(__name__)


def _normalize_content(content: Any) -> str:
    """Extract text from typed JSON content which may be a string or list of parts."""
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                text = str(part["text"]).strip()
                if text:
                    text_parts.append(text)
        return " ".join(text_parts)
    return str(content or "")


def _parse_timestamp(timestamp_str: Any) -> datetime:
    if not isinstance(timestamp_str, str) or not timestamp_str:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


@dataclass
class _ThoughtParts:
    """Separated subject and description from a thoughts array."""

    subject: str
    description: str


def _extract_thought_parts(thoughts: list[Any]) -> list[_ThoughtParts]:
    """Split native-session thoughts into visible headings and thinking descriptions."""
    result: list[_ThoughtParts] = []
    for thought in thoughts:
        if not isinstance(thought, dict):
            continue
        subject = str(thought.get("subject", "")).strip()
        desc = str(thought.get("description", "")).strip()
        if desc:
            if desc.startswith("\\n"):
                desc = desc[2:]
            desc = desc.lstrip("\n").strip()
        if subject or desc:
            result.append(_ThoughtParts(subject=subject, description=desc))
    return result


class TypedJsonTranscriptParser(BaseTranscriptParser):
    """Shared JSONL/native JSON parser for providers with typed message payloads."""

    jsonl_init_event_type: ClassVar[str] = "init"
    jsonl_message_event_type: ClassVar[str] = "message"
    jsonl_result_event_type: ClassVar[str] = "result"
    jsonl_tool_use_event_type: ClassVar[str] = "tool_use"
    jsonl_tool_result_event_type: ClassVar[str] = "tool_result"
    jsonl_user_event_type: ClassVar[str] = "user"
    jsonl_assistant_event_type: ClassVar[str] = "model"

    session_user_message_type: ClassVar[str] = "user"
    session_assistant_message_type: ClassVar[str] = "gemini"
    ignored_session_message_types: ClassVar[tuple[str, ...]] = ("info", "warning")

    def __init__(
        self,
        *,
        cli_name: str,
        session_id: str | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        super().__init__(cli_name=cli_name, session_id=session_id, logger_instance=logger_instance)
        self._last_tool_use_id: str | None = None

    def _tool_use_id_prefix(self) -> str:
        return self.cli_name

    def _next_tool_use_id(
        self,
        data_id: str | None = None,
        *,
        index: int,
        tool_name: str | None,
    ) -> str:
        """Generate or extract a tool_use_id for pairing tool_use with tool_result."""
        if data_id:
            return str(data_id)
        payload = "|".join((self.cli_name, self.session_id or "", str(index), tool_name or ""))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{self._tool_use_id_prefix()}-tu-{digest}"

    def _message_id_for(self, prefix: str, index: int, raw_id: Any = None) -> str:
        """Generate a stable message identifier for deduping token events."""
        session_prefix = self.session_id or self.cli_name
        if isinstance(raw_id, str) and raw_id.strip():
            normalized = raw_id.strip()
            return f"{session_prefix}:{prefix}:{index}:{normalized}"
        return f"{session_prefix}:{prefix}:{index}"

    def _normalize_role(self, role: str) -> str:
        if role == self.jsonl_assistant_event_type:
            return "assistant"
        return role

    def _extract_usage(self, data: dict[str, Any]) -> TokenUsage | None:
        return None

    def extract_last_messages(
        self, turns: list[dict[str, Any]], num_pairs: int = 2
    ) -> list[dict[str, Any]]:
        """Extract last N user<>agent message pairs."""
        messages: list[dict[str, str]] = []
        for turn in reversed(turns):
            event_type = turn.get("type")
            role: str | None = None
            content: Any = None

            if event_type == self.jsonl_message_event_type:
                raw_role = turn.get("role")
                role = str(raw_role) if raw_role else None
                content = turn.get("content")
            elif event_type in (self.jsonl_init_event_type, self.jsonl_result_event_type):
                continue
            elif event_type == self.jsonl_tool_use_event_type:
                role = "assistant"
                tool_name = turn.get("tool_name") or turn.get("function_name", "unknown")
                content = f"[Tool call: {tool_name}]"
            elif event_type == self.jsonl_tool_result_event_type:
                continue
            elif event_type == self.session_user_message_type:
                role = "user"
                raw = turn.get("content")
                if isinstance(raw, list):
                    content = " ".join(
                        item.get("text", "") if isinstance(item, dict) else str(item)
                        for item in raw
                    )
                else:
                    content = raw
            elif event_type == self.session_assistant_message_type:
                role = "assistant"
                content = turn.get("content", "")
            else:
                continue

            if role in ("user", self.jsonl_assistant_event_type, "assistant"):
                norm_role = self._normalize_role(role)

                if isinstance(content, list):
                    content = " ".join(str(part) for part in content)

                messages.insert(0, {"role": norm_role, "content": str(content or "")})
                if len(messages) >= num_pairs * 2:
                    break
        return messages

    def extract_turns_since_clear(
        self, turns: list[dict[str, Any]], max_turns: int | None = None
    ) -> list[dict[str, Any]]:
        """Extract turns since the most recent session boundary."""
        return turns[-max_turns:] if max_turns and len(turns) > max_turns else turns

    def is_session_boundary(self, turn: dict[str, Any]) -> bool:
        """Check if a turn is a session boundary."""
        return False

    def parse_line(self, line: str, index: int) -> ParsedMessage | None:
        """Parse a single JSONL event from a typed transcript."""
        if not line.strip():
            return None

        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            self.error_log.log_malformed_line(
                line_num=index,
                session_id=self.session_id,
                raw_text=line,
                error=str(e),
            )
            return None

        if not isinstance(data, dict):
            self.error_log.log_malformed_line(
                line_num=index,
                session_id=self.session_id,
                raw_text=line,
                error="Line is not a JSON object",
            )
            return None

        timestamp = _parse_timestamp(data.get("timestamp"))
        event_type = data.get("type")

        role: str | None = None
        content: Any = ""
        content_type = "text"
        tool_name: str | None = None
        tool_input: dict[str, Any] | None = None
        tool_result: dict[str, Any] | None = None
        tool_use_id: str | None = None

        if event_type == self.jsonl_init_event_type:
            return None

        if event_type == self.jsonl_message_event_type:
            raw_role = data.get("role")
            role = str(raw_role) if raw_role else None
            content = data.get("content", "")

        elif event_type in (self.jsonl_user_event_type, self.jsonl_assistant_event_type):
            role = str(event_type)
            content = data.get("content", "")

        elif event_type == self.jsonl_tool_use_event_type:
            role = "assistant"
            content_type = "tool_use"
            tool_name = data.get("tool_name") or data.get("function_name")
            tool_input = data.get("parameters") or data.get("args") or data.get("input")
            content = f"Tool call: {tool_name}"
            tool_use_id = self._next_tool_use_id(
                data.get("id") or data.get("tool_call_id"),
                index=index,
                tool_name=tool_name,
            )
            self._last_tool_use_id = tool_use_id

        elif event_type == self.jsonl_tool_result_event_type:
            role = "tool"
            content_type = "tool_result"
            tool_name = data.get("tool_name")
            output = data.get("output") or data.get("result") or ""
            tool_result = {"output": output, "status": data.get("status", "unknown")}
            content = str(output)[:500] if output else ""
            tool_use_id = data.get("tool_id") or data.get("tool_use_id") or self._last_tool_use_id

        elif event_type == self.jsonl_result_event_type:
            return None

        else:
            block_type = str(event_type or "<missing>")
            logger.debug("Unknown %s event type: %s", self.cli_name, event_type)
            return _unknown_block_message(
                index=index,
                block_type=block_type,
                raw=data,
                role="assistant",
                timestamp=timestamp,
                message_id=self._message_id_for("jsonl", index, data.get("id")),
                usage=self._extract_usage(data),
            )

        if not role:
            return None

        role = self._normalize_role(role)

        if isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    text_parts.append(part)
                elif isinstance(part, dict):
                    if "text" in part:
                        text_parts.append(str(part["text"]))
                    if "functionCall" in part and isinstance(part["functionCall"], dict):
                        function_call = part["functionCall"]
                        content_type = "tool_use"
                        raw_tool_name = function_call.get("name")
                        tool_name = str(raw_tool_name) if raw_tool_name else None
                        raw_tool_input = function_call.get("args")
                        tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else None
                        tool_use_id = self._next_tool_use_id(
                            function_call.get("id") or part.get("id"),
                            index=index,
                            tool_name=tool_name,
                        )
                        self._last_tool_use_id = tool_use_id
            content = " ".join(text_parts)
        else:
            content = str(content or "")

        return ParsedMessage(
            index=index,
            role=role,
            content=content,
            content_type=content_type,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
            timestamp=timestamp,
            raw_json=data,
            usage=self._extract_usage(data),
            tool_use_id=tool_use_id,
            message_id=self._message_id_for("jsonl", index, data.get("id")),
        )

    def iter_parse_events(
        self, raw_lines: Iterable[RawLine], start_index: int = 0
    ) -> Iterator[ParseEvent]:
        """Stream events with per-ParsedMessage indexing."""
        current_index = start_index
        for raw in raw_lines:
            message = self.parse_line(raw.text, current_index)
            if message:
                records = annotate_record_source(
                    [message],
                    source=self.cli_name,
                    raw_line_no=raw.raw_line_no,
                )
                yield ParseEvent(
                    byte_offset=raw.byte_offset,
                    raw_line_no=raw.raw_line_no,
                    parsed_index=current_index,
                    records=records,
                    parser_safe=True,
                )
                current_index += 1

    def parse_session_json(self, data: dict[str, Any]) -> list[ParsedMessage]:
        """Parse a native JSON session file."""
        if not isinstance(data, dict):
            return []
        messages = data.get("messages", [])
        if not isinstance(messages, list):
            return []
        parsed: list[ParsedMessage] = []
        index = 0

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            result = self._parse_session_message(msg, index)
            if result:
                parsed.extend(result)
                index += len(result)

        return parsed

    def _parse_session_message(self, msg: dict[str, Any], start_index: int) -> list[ParsedMessage]:
        """Parse a single message from a native JSON session file."""
        msg_type = msg.get("type")
        content = msg.get("content", "")
        timestamp = _parse_timestamp(msg.get("timestamp"))

        if msg_type == self.session_user_message_type:
            normalized = _normalize_content(content)
            return [
                ParsedMessage(
                    index=start_index,
                    role="user",
                    content=normalized,
                    content_type="text",
                    tool_name=None,
                    tool_input=None,
                    tool_result=None,
                    timestamp=timestamp,
                    raw_json=msg,
                    usage=self._extract_usage(msg),
                    message_id=self._message_id_for("json", start_index, msg.get("id")),
                )
            ]

        if msg_type == self.session_assistant_message_type:
            return self._parse_session_assistant_message(msg, start_index, content, timestamp)

        if msg_type in self.ignored_session_message_types:
            return []

        return [
            _unknown_block_message(
                index=start_index,
                block_type=str(msg_type or "<missing>"),
                raw=msg,
                role="assistant",
                timestamp=timestamp,
                message_id=self._message_id_for("json", start_index, msg.get("id")),
                usage=self._extract_usage(msg),
            )
        ]

    def _parse_session_assistant_message(
        self,
        msg: dict[str, Any],
        start_index: int,
        content: Any,
        timestamp: datetime,
    ) -> list[ParsedMessage]:
        results: list[ParsedMessage] = []
        idx = start_index
        usage = self._extract_usage(msg)

        def consume_usage() -> TokenUsage | None:
            nonlocal usage
            current = usage
            usage = None
            return current

        thoughts = msg.get("thoughts")
        if isinstance(thoughts, list) and thoughts:
            segments: list[str] = []
            for tp in _extract_thought_parts(thoughts):
                if tp.subject and tp.description:
                    segments.append(f"**{tp.subject}**\n\n{tp.description}")
                elif tp.subject:
                    segments.append(f"**{tp.subject}**")
                elif tp.description:
                    segments.append(tp.description)
            if segments:
                results.append(
                    ParsedMessage(
                        index=idx,
                        role="assistant",
                        content="\n\n".join(segments),
                        content_type="thinking",
                        tool_name=None,
                        tool_input=None,
                        tool_result=None,
                        timestamp=timestamp,
                        raw_json=msg,
                        usage=consume_usage(),
                        message_id=self._message_id_for("json", idx, msg.get("id")),
                    )
                )
                idx += 1

        normalized_content = _normalize_content(content)
        if normalized_content:
            results.append(
                ParsedMessage(
                    index=idx,
                    role="assistant",
                    content=normalized_content,
                    content_type="text",
                    tool_name=None,
                    tool_input=None,
                    tool_result=None,
                    timestamp=timestamp,
                    raw_json=msg,
                    usage=consume_usage(),
                    message_id=self._message_id_for("json", idx, msg.get("id")),
                )
            )
            idx += 1

        tool_calls = msg.get("toolCalls", [])
        if not isinstance(tool_calls, list):
            tool_calls = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            raw_tool_name = tc.get("name", "unknown")
            tool_name = str(raw_tool_name) if raw_tool_name else "unknown"
            raw_tool_args = tc.get("args")
            tool_args = raw_tool_args if isinstance(raw_tool_args, dict) else None
            tc_id = self._next_tool_use_id(
                tc.get("id"),
                index=idx,
                tool_name=tool_name,
            )

            results.append(
                ParsedMessage(
                    index=idx,
                    role="assistant",
                    content=f"Tool call: {tool_name}",
                    content_type="tool_use",
                    tool_name=tool_name,
                    tool_input=tool_args,
                    tool_result=None,
                    timestamp=timestamp,
                    raw_json=tc,
                    usage=consume_usage(),
                    tool_use_id=tc_id,
                    message_id=self._message_id_for("json", idx, tc.get("id")),
                )
            )
            idx += 1

            result_value = tc.get("result")
            func_response_found = False
            func_response = None
            if isinstance(result_value, list) and result_value:
                first = result_value[0]
                if isinstance(first, dict) and "functionResponse" in first:
                    func_response_found = True
                    func_response = first.get("functionResponse")
            elif isinstance(result_value, dict) and "functionResponse" in result_value:
                func_response_found = True
                func_response = result_value.get("functionResponse")
            if func_response_found:
                output = str(func_response)[:500]
                results.append(
                    ParsedMessage(
                        index=idx,
                        role="tool",
                        content=output,
                        content_type="tool_result",
                        tool_name=tool_name,
                        tool_input=None,
                        tool_result={"output": func_response, "status": "success"},
                        timestamp=timestamp,
                        raw_json=tc,
                        usage=consume_usage(),
                        tool_use_id=tc_id,
                        message_id=self._message_id_for("json", idx, tc.get("id")),
                    )
                )
                idx += 1

        return results
