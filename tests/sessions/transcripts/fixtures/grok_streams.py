"""Synthetic Grok ``updates.jsonl`` streams matching the four audited shapes."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.sessions.transcripts.base import TokenUsage

DAEMON_WAKE_BODY = "Message from Gobby daemon: New activity available. Continue the assigned goal."
WAIT_DIRECTIVE_BODY = (
    "Continue where you last left off. The previous turn called "
    "`gobby-sessions:compact_self`. If startup context is missing, call "
    'gobby-sessions.wait_for_summary(session_id="fixture"). If it returns '
    "`completed=false`, repeat the same wait call. Once complete, continue."
)
_MID_TURN_INJECTION = "The user sent a message while you were working"
_METHODS = ("session/update", "_x.ai/session/update")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tool_input: dict[str, Any]
    status: str = "completed"
    output: str = "ok"
    use_fallback_keys: bool = False


@dataclass(frozen=True)
class TurnSpec:
    prompt: str | None
    injections: tuple[str, ...] = ()
    agent_blocks: tuple[str, ...] = ()
    thought_blocks: int = 0
    tool_calls: int = 0
    tools: tuple[ToolSpec, ...] = ()
    stop_reason: str | None = "end_turn"
    usage: dict[str, int] | None = None
    compaction_restart: bool = False
    wake_prompt: bool = False


def map_turn_usage(usage: dict[str, int] | None) -> TokenUsage | None:
    """Apply the grok ``turn_completed`` cache split to a fixture usage dict."""
    if usage is None:
        return None
    cache_read = int(usage.get("cachedReadTokens", 0))
    cache_creation = int(usage.get("cacheCreationTokens", 0))
    input_tokens = max(0, int(usage.get("inputTokens", 0)) - cache_read - cache_creation)
    output_tokens = int(usage.get("outputTokens", 0))
    if input_tokens == output_tokens == cache_read == cache_creation == 0:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
    )


def expected_token_usage(turns: Sequence[TurnSpec]) -> TokenUsage:
    """Sum mapped turn-aggregate usage for an audited stream."""
    total = TokenUsage()
    for spec in turns:
        part = map_turn_usage(spec.usage)
        if part is None:
            continue
        total.input_tokens += part.input_tokens
        total.output_tokens += part.output_tokens
        total.cache_creation_tokens += part.cache_creation_tokens
        total.cache_read_tokens += part.cache_read_tokens
    return total


def build_stream(turns: list[TurnSpec], *, session_id: str = "grok-fixture") -> list[str]:
    """Render JSONL lines in the real ``updates.jsonl`` envelope."""
    lines: list[str] = []
    seq = 0

    def emit(update: dict[str, Any]) -> None:
        nonlocal seq
        method = _METHODS[seq % 2]
        seq += 1
        stamped = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seq)
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {"sessionId": session_id, "update": update},
            "timestamp": stamped.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        lines.append(json.dumps(payload, separators=(",", ":")))

    for turn_i, spec in enumerate(turns):
        if spec.compaction_restart:
            emit({"sessionUpdate": "compaction_checkpoint", "kind": "restart"})
            emit({"sessionUpdate": "auto_compact_completed"})
        if spec.wake_prompt:
            emit(_text_update("user_message_chunk", DAEMON_WAKE_BODY))
        elif spec.prompt is not None:
            emit(_text_update("user_message_chunk", spec.prompt))

        for thought_i in range(spec.thought_blocks):
            emit(_text_update("agent_thought_chunk", f"thought-{turn_i}-{thought_i}"))

        parts = _partition_around_injections(spec.agent_blocks, spec.injections)
        for part_i, part in enumerate(parts):
            if part_i > 0:
                emit(_text_update("user_message_chunk", spec.injections[part_i - 1]))
            for block in part:
                emit(_text_update("agent_message_chunk", block))
            if part_i == 0:
                _emit_tools(
                    emit,
                    session_id=session_id,
                    turn_i=turn_i,
                    count=spec.tool_calls,
                    tools=spec.tools,
                )

        if spec.stop_reason is not None:
            completed: dict[str, Any] = {
                "sessionUpdate": "turn_completed",
                "stop_reason": spec.stop_reason,
                "prompt_id": f"{session_id}-p{turn_i}",
            }
            if spec.usage is not None:
                completed["usage"] = spec.usage
            emit(completed)
    return lines


def _text_update(session_update: str, text: str) -> dict[str, Any]:
    return {"sessionUpdate": session_update, "content": {"type": "text", "text": text}}


def _emit_tools(
    emit: Callable[[dict[str, Any]], None],
    *,
    session_id: str,
    turn_i: int,
    count: int,
    tools: tuple[ToolSpec, ...],
) -> None:
    default_tools = tuple(ToolSpec("grep", {"pattern": "fixture"}) for _ in range(count))
    for tool_i, tool in enumerate((*default_tools, *tools)):
        call_id = f"{session_id}-t{turn_i}-{tool_i}"
        name_key = "name" if tool.use_fallback_keys else "title"
        input_key = "input" if tool.use_fallback_keys else "rawInput"
        emit(
            {
                "sessionUpdate": "tool_call",
                name_key: tool.name,
                "toolCallId": call_id,
                input_key: tool.tool_input,
            }
        )
        emit(
            {
                "sessionUpdate": "tool_call_update",
                "status": tool.status,
                "toolCallId": call_id,
                "content": {"type": "text", "text": tool.output},
            }
        )
        emit({"sessionUpdate": "hook_annotation", "hook": "PostToolUse"})
        emit(
            {
                "sessionUpdate": "hook_execution",
                "hook": "PostToolUse",
                "content": {"type": "text", "text": "hook-ok"},
            }
        )


def _partition_around_injections(
    blocks: tuple[str, ...], injections: tuple[str, ...]
) -> list[tuple[str, ...]]:
    parts = len(injections) + 1
    if not injections:
        return [blocks]
    size = len(blocks)
    base, extra = divmod(size, parts)
    result: list[tuple[str, ...]] = []
    index = 0
    for part_i in range(parts):
        take = base + (1 if part_i < extra else 0)
        result.append(blocks[index : index + take])
        index += take
    return result


def _usage(seed: int, *, model_calls: int = 2, output: int = 20) -> dict[str, int]:
    return {
        "inputTokens": 100 + seed,
        "outputTokens": output,
        "cachedReadTokens": 10,
        "cacheCreationTokens": 5,
        "modelCalls": model_calls,
    }


def _sized(length: int, tag: str) -> str:
    prefix = f"{tag}:"
    if length <= len(prefix):
        return tag[:length]
    return prefix + ("x" * (length - len(prefix)))


def _session_10695_turns() -> tuple[TurnSpec, ...]:
    turns: list[TurnSpec] = []
    block_index = 0
    completed = 0

    def take_blocks() -> tuple[str, ...]:
        nonlocal block_index, completed
        count = 9 if completed < 5 else 8
        blocks = tuple(f"blk{block_index + offset}" for offset in range(count))
        block_index += count
        completed += 1
        return blocks

    for index in range(17):
        turns.append(
            TurnSpec(
                prompt=None,
                agent_blocks=take_blocks(),
                thought_blocks=1,
                tool_calls=1 if index % 5 == 0 else 0,
                stop_reason="end_turn",
                usage=_usage(index, model_calls=2),
                compaction_restart=True,
                wake_prompt=True,
            )
        )
    for index in range(18):
        turns.append(
            TurnSpec(
                prompt=f"Real prompt 10695-{index}: implement the next leaf.",
                agent_blocks=take_blocks(),
                thought_blocks=1,
                tool_calls=1 if (17 + index) % 5 == 0 else 0,
                stop_reason="end_turn",
                usage=_usage(17 + index, model_calls=3),
            )
        )
    for index in range(20):
        turns.append(
            TurnSpec(
                prompt=f"Cancelled prompt 10695-{index}: this was interrupted.",
                stop_reason="cancelled",
                usage=_usage(100 + index, model_calls=1, output=0),
            )
        )
    return tuple(turns)


def _session_10715_turns() -> tuple[TurnSpec, ...]:
    marathon_blocks = tuple(_sized(222, f"m{index:02d}") for index in range(50))
    return (
        TurnSpec(
            prompt="Marathon task: finish the long implementation in one turn.",
            injections=(
                f"{_MID_TURN_INJECTION}: keep going on part A.",
                f"{_MID_TURN_INJECTION}: also handle part B.",
            ),
            agent_blocks=marathon_blocks,
            thought_blocks=2,
            tool_calls=1,
            stop_reason="end_turn",
            usage={
                "inputTokens": 12000,
                "outputTokens": 4000,
                "cachedReadTokens": 3000,
                "cacheCreationTokens": 200,
                "modelCalls": 459,
            },
        ),
        TurnSpec(
            prompt="Local command: run the focused parser tests.",
            agent_blocks=("local-command-done",),
            stop_reason="end_turn",
            usage=None,
        ),
        TurnSpec(
            prompt="Small follow-up: confirm the usage split.",
            agent_blocks=("follow-up-a",),
            stop_reason="end_turn",
            usage=_usage(2, model_calls=1),
        ),
        TurnSpec(
            prompt="Small wrap-up: close the session notes.",
            agent_blocks=("follow-up-b",),
            stop_reason="end_turn",
            usage=_usage(3, model_calls=1),
        ),
    )


def _session_10725_turns() -> tuple[TurnSpec, ...]:
    cancelled = [
        TurnSpec(prompt=None, wake_prompt=True, stop_reason="cancelled"),
        *[TurnSpec(prompt=WAIT_DIRECTIVE_BODY, stop_reason="cancelled") for _ in range(6)],
    ]
    completed = [
        TurnSpec(
            prompt=WAIT_DIRECTIVE_BODY,
            agent_blocks=(f"synthetic-continue-{index}",),
            stop_reason="end_turn",
            usage=_usage(index, model_calls=1),
        )
        for index in range(2)
    ]
    return tuple(cancelled + completed)


def _session_10711_turns() -> tuple[TurnSpec, ...]:
    real = [
        TurnSpec(
            prompt=f"Real user request {index}: add fixture coverage.",
            agent_blocks=(f"done-{index}",),
            thought_blocks=1 if index == 0 else 0,
            tools=(
                (
                    ToolSpec(
                        "search_replace",
                        {
                            "target_file": "/repo/widget.py",
                            "old_string": "old",
                            "new_string": "new",
                        },
                        use_fallback_keys=True,
                    ),
                    ToolSpec(
                        "use_tool",
                        {
                            "tool_name": "call_tool",
                            "tool_input": {
                                "server_name": "gobby-tasks",
                                "tool_name": "claim_task",
                                "arguments": {"task_id": "#20728"},
                            },
                        },
                    ),
                    ToolSpec(
                        "run_terminal_command",
                        {"command": "uv run pytest -k widget"},
                        status="failed",
                        output="exit 1",
                    ),
                )
                if index == 0
                else ()
            ),
            stop_reason="end_turn",
            usage=_usage(index, model_calls=2),
        )
        for index in range(5)
    ]
    synthetic = [
        TurnSpec(prompt=None, wake_prompt=True, stop_reason="cancelled"),
        TurnSpec(prompt=WAIT_DIRECTIVE_BODY, stop_reason="cancelled"),
        TurnSpec(prompt=WAIT_DIRECTIVE_BODY, stop_reason="cancelled"),
    ]
    return tuple(real + synthetic)


SESSION_10695_TURNS = _session_10695_turns()
SESSION_10715_TURNS = _session_10715_turns()
SESSION_10725_TURNS = _session_10725_turns()
SESSION_10711_TURNS = _session_10711_turns()

# True turn_completed counts from the 2026-08-18 audit shapes.
SESSION_10695_TURN_COUNT = 55
SESSION_10715_TURN_COUNT = 4
SESSION_10725_TURN_COUNT = 9
SESSION_10711_TURN_COUNT = 8

# extract_last_messages adjacency pairs (no digest synthetic filter).
# 10695: 35 joined completions + 20 cancelled empty assistants.
# 10715: marathon prompt + 2 injections (3 groups, each <4000) + 3 small turns.
# 10725: 7 cancelled empty + 2 synthetic completions.
# 10711: 5 real completions + 3 cancelled empty.
SESSION_10695_PAIR_COUNT = 55
SESSION_10715_PAIR_COUNT = 6
SESSION_10725_PAIR_COUNT = 9
SESSION_10711_PAIR_COUNT = 8

SESSION_10695_USAGE = expected_token_usage(SESSION_10695_TURNS)
SESSION_10715_USAGE = expected_token_usage(SESSION_10715_TURNS)
SESSION_10725_USAGE = TokenUsage(
    input_tokens=171,
    output_tokens=40,
    cache_creation_tokens=10,
    cache_read_tokens=20,
)
SESSION_10711_USAGE = expected_token_usage(SESSION_10711_TURNS)


def session_10695_shape() -> list[str]:
    """55 turns: 35 end_turn, 20 cancelled, 17 compaction+wake restarts, 285 blocks."""
    return build_stream(list(SESSION_10695_TURNS), session_id="session-10695")


def session_10715_shape() -> list[str]:
    """4 turns: marathon (50 blocks, 2 injections, 459 modelCalls) plus 3 small."""
    return build_stream(list(SESSION_10715_TURNS), session_id="session-10715")


def session_10725_shape() -> list[str]:
    """9 turns (7 cancelled); every prompt is classifier-true synthetic."""
    return build_stream(list(SESSION_10725_TURNS), session_id="session-10725")


def session_10711_shape() -> list[str]:
    """8 turns (3 cancelled) with 5 real prompts."""
    return build_stream(list(SESSION_10711_TURNS), session_id="session-10711")
