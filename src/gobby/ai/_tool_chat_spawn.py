"""Family B *spawn* adapters for the daemon ``tool_chat`` capability.

These adapters run an EXTERNAL agent CLI (Codex, Droid, Grok) in its own
agentic loop inside the provider's native OS sandbox, hand it a prompt that
instructs it to investigate the indexed codebase by running the ``gcode`` CLI
directly via shell, and capture the agent's final message as the grounded
narrative. The daemon never runs the agent's loop.

**gcode-direct** — every spawn provider is sandboxed:

* **codex** — ``codex exec --sandbox workspace-write`` (Seatbelt on macOS).
* **droid** — ``droid exec`` default read-only autonomy.
* **grok** — ``grok --single --sandbox workspace``.

The agent runs in a neutral temp working directory (never the target repo),
so the target repo is byte-identical after a run. ``gcode`` reads the Postgres
index, not the working tree, so the agent passes ``--project <repo>`` to scope
its queries. Read-only is enforced by the sandbox plus ``validate_policy``
(which rejects mutating subcommands before the spawn).

Dispatch stays purely on :class:`AIAdapterStyle`; provider names live only in
the concrete adapter classes here and in the builder factory map, never in the
service, route, or call-site. Grok uses ``AIAdapterStyle.ACP``; the
:class:`ACPSpawnToolChatAdapter` composite checks ``binding.provider`` inside
``chat()`` (provider names are allowed in the adapter layer).
"""

from __future__ import annotations

import json
import logging
import shlex
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from gobby.agents.spawn_cache_policy import merge_spawn_path
from gobby.ai._text_generation_adapters import (
    _extend_reasoning_args,
    _run_cli_text_generation_command,
)
from gobby.ai._tool_chat_codex import CodexSpawnToolChatAdapter
from gobby.ai._tool_chat_contracts import (
    ToolChatRequest,
    ToolChatResult,
)
from gobby.ai._tool_chat_droid import DroidSpawnToolChatAdapter
from gobby.ai._tool_chat_tools import validate_policy

__all__ = [
    "CodexSpawnToolChatAdapter",
    "DroidSpawnToolChatAdapter",
    "GrokSpawnToolChatAdapter",
]

if TYPE_CHECKING:
    from gobby.ai.registry import CapabilityBinding
    from gobby.config.app import DaemonConfig

_DEFAULT_SPAWN_TIMEOUT_SECONDS = 300.0

logger = logging.getLogger(__name__)

# Grok built-in tools disabled for tool_chat spawn agents. The ``--sandbox
# workspace`` profile confines writes to the neutral temp cwd; these disabled
# tools are defense-in-depth on top of the sandbox.
_GROK_DISABLED_TOOLS = "Edit,Write,MultiEdit,NotebookEdit,Agent,Task"


def compose_gcode_direct_prompt(request: ToolChatRequest) -> str:
    """Compose the seed prompt plus a gcode-direct investigation preamble.

    Shared by all three spawn adapters. The agent runs ``gcode`` directly via
    shell in its sandbox, passing ``--project`` to scope queries to the target
    repo (the agent's cwd is a neutral temp dir, not the repo).
    """
    cli = request.tool_policy.cli
    subcommands = ", ".join(request.tool_policy.tools)
    project = shlex.quote(request.project_path)
    preamble = (
        f"You are investigating an indexed codebase via the read-only `{cli}` "
        f"CLI. Run `{cli} <subcommand> --project {project} <args>` in the shell "
        f"to gather evidence. Available read-only subcommands: {subcommands}. "
        f"Always pass --project {project}. Ground every claim in `file:line` "
        f"citations from the index. Do NOT modify anything. Output ONLY the "
        f"finished documentation, with no preamble or tool transcripts."
    )
    parts = [part for part in (request.system_prompt, request.prompt, preamble) if part]
    return "\n\n".join(parts)


def _resolve_grok_session_dir(session_id: str, work_dir: Path) -> Path | None:
    """Resolve the grok session directory for a given session ID and cwd.

    Grok stores sessions under ``~/.grok/sessions/<url-encoded-cwd>/<session-id>/``.
    The cwd is URL-encoded with ``/`` -> ``%2F`` (i.e. ``quote(path, safe='')``).
    Falls back to a recursive glob under ``~/.grok/sessions/`` if the computed
    path does not exist (handles edge cases where grok resolves symlinks
    differently).
    """
    sessions_root = Path.home() / ".grok" / "sessions"
    encoded_cwd = quote(str(work_dir), safe="")
    direct = sessions_root / encoded_cwd / session_id
    if direct.is_dir():
        return direct
    # Fallback: search by session ID across all encoded-cwd directories.
    for candidate in sessions_root.rglob(session_id):
        if candidate.is_dir():
            return candidate
    return None


def parse_grok_session_signals(
    session_dir: Path,
) -> tuple[int, dict[str, int], int | None]:
    """Extract tool calls, per-tool counts, and native turns from a Grok session.

    Reads ``signals.json`` for aggregate ``toolCallCount`` and ``turnCount``, and
    ``updates.jsonl`` for per-tool counts. Missing provider data stays unknown.
    """
    total = 0
    breakdown: dict[str, int] = {}
    turns: int | None = None
    signals: dict[str, Any] = {}

    signals_path = session_dir / "signals.json"
    if signals_path.exists():
        try:
            raw_signals = json.loads(signals_path.read_text(encoding="utf-8"))
            if isinstance(raw_signals, dict):
                signals = raw_signals
                raw_turns = signals.get("turnCount")
                if isinstance(raw_turns, int):
                    turns = raw_turns
        except (json.JSONDecodeError, TypeError):
            pass

    # Primary source: updates.jsonl has per-tool-call records.
    updates_path = session_dir / "updates.jsonl"
    if updates_path.exists():
        for raw in updates_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("method") != "session/update":
                continue
            params = event.get("params")
            if not isinstance(params, dict):
                continue
            update = params.get("update")
            if not isinstance(update, dict):
                continue
            if update.get("sessionUpdate") != "tool_call":
                continue
            title = str(update.get("title") or "tool")
            total += 1
            breakdown[title] = breakdown.get(title, 0) + 1
        if total > 0:
            return total, breakdown, turns

    # Fallback: signals.json has the aggregate count but no per-tool counts.
    count = signals.get("toolCallCount")
    if isinstance(count, int) and count > 0:
        tools_used = signals.get("toolsUsed")
        if isinstance(tools_used, list):
            # Without per-tool counts from updates.jsonl, retain which tools ran.
            for tool_name in tools_used:
                breakdown[str(tool_name)] = breakdown.get(str(tool_name), 0) + 1
        return count, breakdown, turns

    return 0, {}, turns


def _normalize_grok_stop_reason(value: object) -> str | None:
    if value == "EndTurn":
        return "completed"
    if value == "MaxTurnRequests":
        return "max_turns"
    return None


class GrokSpawnToolChatAdapter:
    """Family B adapter for the ``acp`` style — Grok via ``grok --single``.

    Spawns ``grok --single`` in a neutral working directory with the
    ``workspace`` sandbox profile, ``--always-approve`` for headless tool
    execution, and mutating built-in tools disabled. Grok runs its own agentic
    loop, investigates by running ``gcode`` via shell, and emits the final
    narrative as a single JSON object on stdout (``--output-format json``).
    Tool-call provenance is extracted from the persisted session directory
    (``signals.json`` and ``updates.jsonl``) after the run completes, since the
    CLI output formats do not include tool-call events.
    """

    def __init__(
        self,
        *,
        command_path: str | None = None,
        timeout_seconds: float = _DEFAULT_SPAWN_TIMEOUT_SECONDS,
    ) -> None:
        self._command_path = command_path
        self._timeout_seconds = timeout_seconds

    def _resolve_command_path(self) -> str:
        import shutil

        path = self._command_path or shutil.which("grok")
        if not path:
            raise FileNotFoundError("Grok CLI not found in PATH")
        return path

    def _build_command(self, request: ToolChatRequest, *, model: str | None) -> list[str]:
        limits = request.effective_limits
        command = [
            self._resolve_command_path(),
            "--single",
            compose_gcode_direct_prompt(request),
            "--output-format",
            "json",
            "--sandbox",
            "workspace",
            "--always-approve",
            "--no-subagents",
            "--no-memory",
            "--disallowed-tools",
            _GROK_DISABLED_TOOLS,
        ]
        if limits.max_turns is not None:
            command.extend(["--max-turns", str(limits.max_turns)])
        if model:
            command.extend(["--model", model])
        _extend_reasoning_args(command, "grok", request.reasoning_effort)
        return command

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        validate_policy(request.tool_policy)
        model = request.model or next(iter(binding.models), None)
        command = self._build_command(request, model=model)
        with tempfile.TemporaryDirectory(prefix="tool-chat-grok-") as work_str:
            work = Path(work_str)
            stdout = await _run_cli_text_generation_command(
                "Grok tool_chat",
                command,
                neutral_cwd=work,
                timeout_seconds=self._timeout_seconds,
                env_overrides={
                    "PATH": merge_spawn_path(None),
                    **request.managed_subprocess_env,
                },
            )
        text = ""
        session_id = ""
        stop_reason: str | None = None
        try:
            result = json.loads(stdout)
            if isinstance(result, dict):
                text = (result.get("text") or "").strip()
                stop_reason = _normalize_grok_stop_reason(result.get("stopReason"))
                sid = result.get("sessionId")
                if isinstance(sid, str):
                    session_id = sid
        except (json.JSONDecodeError, TypeError):
            pass
        if not text:
            raise RuntimeError(f"Grok tool_chat produced no final message (model={model})")
        # Extract tool-call provenance from the persisted session directory.
        tool_use_count = 0
        tools: dict[str, int] = {}
        turns: int | None = None
        if session_id:
            session_dir = _resolve_grok_session_dir(session_id, work)
            if session_dir is not None:
                tool_use_count, tools, turns = parse_grok_session_signals(session_dir)
        return ToolChatResult(
            text=text,
            provider=binding.provider,
            model=model,
            tool_use_count=tool_use_count,
            turns=turns,
            tools=tools,
            applied_reasoning_effort=(
                request.reasoning_effort if request.reasoning_effort != "auto" else None
            ),
            stop_reason=stop_reason,
            trace=(),
            calls_used=0,
            budget_exhausted=False,
            trace_available=False,
        )


class ACPSpawnToolChatAdapter:
    """Composite Family B adapter for the ``acp`` style (Grok).

    The tool_chat service dispatches on ``AIAdapterStyle`` and Grok maps to
    ``ACP``. This composite checks ``binding.provider`` inside ``chat()`` so an
    unexpected ACP provider fails loudly instead of running the Grok CLI.
    """

    def __init__(self, config: DaemonConfig) -> None:
        timeout = config.ai.generation.timeout_seconds
        self._grok = GrokSpawnToolChatAdapter(timeout_seconds=timeout)

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        if binding.provider == "grok":
            return await self._grok.chat(request, binding)
        raise ValueError(
            f"No ACP tool_chat adapter for provider {binding.provider!r}; expected 'grok'."
        )
