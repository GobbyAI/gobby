"""Family B *spawn* adapters for the daemon ``tool_chat`` capability.

These adapters run an EXTERNAL agent CLI (Codex, Droid, Grok, Qwen) in its own
agentic loop inside the provider's native OS sandbox, hand it a prompt that
instructs it to investigate the indexed codebase by running the ``gcode`` CLI
directly via shell, and capture the agent's final message as the grounded
narrative. The daemon never runs the agent's loop.

**gcode-direct** — every spawn provider is sandboxed:

* **codex** — ``codex exec --sandbox workspace-write`` (Seatbelt on macOS).
* **droid** — ``droid exec`` default read-only autonomy.
* **grok** — ``grok --single --sandbox workspace``.
* **qwen** — ``qwen --sandbox`` (Seatbelt ``gobby-open``, a custom profile
  written to the neutral cwd's ``.qwen/`` to work around a path-resolution
  bug in qwen-code 0.19.x).

The agent runs in a neutral temp working directory (never the target repo),
so the target repo is byte-identical after a run. ``gcode`` reads the Postgres
index, not the working tree, so the agent passes ``--project <repo>`` to scope
its queries. Read-only is enforced by the sandbox plus ``validate_policy``
(which rejects mutating subcommands before the spawn).

Dispatch stays purely on :class:`AIAdapterStyle`; provider names live only in
the concrete adapter classes here and in the builder factory map, never in the
service, route, or call-site. Grok and Qwen share ``AIAdapterStyle.ACP``; the
:class:`ACPSpawnToolChatAdapter` composite dispatches to the correct sub-adapter
by ``binding.provider`` inside ``chat()`` (provider names are allowed in the
adapter layer).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from gobby.agents.spawn_cache_policy import merge_spawn_path
from gobby.ai._text_generation_adapters import (
    _droid_isolated_env,
    _extend_reasoning_args,
    _normalize_qwen_openai_endpoints,
    _run_cli_text_generation_command,
    _seed_droid_factory_state,
)
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolChatResult
from gobby.ai._tool_chat_tools import validate_policy

if TYPE_CHECKING:
    from collections.abc import Mapping

    from gobby.ai.registry import CapabilityBinding
    from gobby.config.app import DaemonConfig

_DEFAULT_SPAWN_TIMEOUT_SECONDS = 300.0

# Droid built-in tools disabled for tool_chat spawn agents. Execute is NOT
# disabled — the agent needs shell access to run gcode. Droid's default
# read-only exec autonomy is the OS sandbox that prevents file writes; the
# remaining disabled tools are defense-in-depth on top of that.
_DROID_DISABLED_TOOLS: tuple[str, ...] = (
    "Edit",
    "ApplyPatch",
    "Create",
    "CreateAutomation",
    "EditAutomation",
    "DeleteAutomation",
    "CronCreate",
    "CronDelete",
    "GenerateDroid",
    "Task",
)

# Grok built-in tools disabled for tool_chat spawn agents. The ``--sandbox
# workspace`` profile confines writes to the neutral temp cwd; these disabled
# tools are defense-in-depth on top of the sandbox.
_GROK_DISABLED_TOOLS = "Edit,Write,MultiEdit,NotebookEdit,Agent,Task"

# Custom seatbelt profile name for the Qwen adapter.  The qwen-code package
# (0.19.x) has a path-resolution bug: ``new URL('sandbox-macos-${profile}.sb',
# import.meta.url)`` in ``chunks/gemini-NDTG7WAX.js`` resolves relative to the
# chunk file inside ``chunks/``, but the ``.sb`` files live in the package
# root — one directory up.  Builtin profile names trigger this broken path.
# Custom (non-builtin) names fall back to ``path.join(".qwen",
# "sandbox-macos-<name>.sb")`` relative to the cwd, which we can satisfy by
# writing the profile into the neutral temp working directory.
_QWEN_SEATBELT_PROFILE_NAME = "gobby-open"

# Permissive-open seatbelt profile content (mirrors qwen-code's
# ``sandbox-macos-permissive-open.sb``).  Allows all operations by default
# except file writes, which are restricted to the sandbox-parameter paths
# (TARGET_DIR, TMP_DIR, QWEN_DIR, etc.).  These parameters are injected by
# qwen-code via ``sandbox-exec -D``.
_QWEN_SEATBELT_PROFILE_CONTENT = """\
(version 1)

;; allow everything by default
(allow default)

;; deny all writes EXCEPT under specific paths
(deny file-write*)
(allow file-write*
    (subpath (param "TARGET_DIR"))
    (subpath (param "TMP_DIR"))
    (subpath (param "CACHE_DIR"))
    (subpath (param "QWEN_DIR"))
    (subpath (param "RUNTIME_DIR"))
    (subpath (string-append (param "HOME_DIR") "/.npm"))
    (subpath (string-append (param "HOME_DIR") "/.cache"))
    (subpath (string-append (param "HOME_DIR") "/.gitconfig"))
    ;; Allow writes to included directories from --include-directories
    (subpath (param "INCLUDE_DIR_0"))
    (subpath (param "INCLUDE_DIR_1"))
    (subpath (param "INCLUDE_DIR_2"))
    (subpath (param "INCLUDE_DIR_3"))
    (subpath (param "INCLUDE_DIR_4"))
    (literal "/dev/stdout")
    (literal "/dev/stderr")
    (literal "/dev/null")
    (literal "/dev/ptmx")
    (regex #"^/dev/ttys[0-9]*$")
)
"""


def _prepare_qwen_sandbox_profile(work_dir: Path) -> None:
    """Write the seatbelt profile into ``<work_dir>/.qwen/`` for the qwen sandbox.

    The qwen-code sandbox initializer looks for custom (non-builtin) profile
    files at ``path.join(".qwen", "sandbox-macos-<name>.sb")`` relative to the
    cwd.  We write the permissive-open profile content there so the sandbox
    activates correctly despite the upstream path-resolution bug.
    """
    qwen_dir = work_dir / ".qwen"
    qwen_dir.mkdir(parents=True, exist_ok=True)
    profile_path = qwen_dir / f"sandbox-macos-{_QWEN_SEATBELT_PROFILE_NAME}.sb"
    profile_path.write_text(_QWEN_SEATBELT_PROFILE_CONTENT, encoding="utf-8")


def compose_gcode_direct_prompt(request: ToolChatRequest) -> str:
    """Compose the seed prompt plus a gcode-direct investigation preamble.

    Shared by all four spawn adapters. The agent runs ``gcode`` directly via
    shell in its sandbox, passing ``--project`` to scope queries to the target
    repo (the agent's cwd is a neutral temp dir, not the repo).
    """
    cli = request.tool_policy.cli
    subcommands = ", ".join(request.tool_policy.tools)
    project = request.project_path
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


def parse_codex_stream(stdout: str) -> tuple[str, int, dict[str, int]]:
    """Parse ``codex exec --json`` JSONL into (final_text, tool_calls, breakdown).

    Tool-call provenance comes from ``item.completed`` events with
    ``item.type == "command_execution"``. The narrative is the
    ``agent_message`` item's ``text`` (used as a fallback when
    ``--output-last-message`` is empty). Non-JSON lines are skipped.
    """
    final_text = ""
    breakdown: dict[str, int] = {}
    total = 0
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "command_execution":
            total += 1
            breakdown["command_execution"] = breakdown.get("command_execution", 0) + 1
        elif item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                final_text = text
    return final_text.strip(), total, breakdown


def parse_qwen_stream(stdout: str) -> tuple[str, int, dict[str, int]]:
    """Parse qwen ``--output-format stream-json`` NDJSON into (final_text, tool_calls, breakdown).

    Tool-call provenance comes from ``assistant`` events whose ``content``
    array contains ``tool_use`` entries (counted by ``name``). The narrative is
    the ``result`` event's ``result`` field. Non-JSON lines are skipped.
    """
    final_text = ""
    breakdown: dict[str, int] = {}
    total = 0
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "assistant":
            # qwen-code nests the assistant message under `message` (Claude-Code
            # stream shape); tool_use blocks live in message.content, not at the
            # top level. Fall back to top-level content for forward-compat.
            message = event.get("message")
            content = (message.get("content") if isinstance(message, dict) else None) or event.get(
                "content"
            )
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = str(block.get("name") or "tool")
                        total += 1
                        breakdown[name] = breakdown.get(name, 0) + 1
        elif etype == "result":
            result = event.get("result")
            if isinstance(result, str) and result.strip():
                final_text = result
    return final_text.strip(), total, breakdown


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


def parse_grok_session_signals(session_dir: Path) -> tuple[int, dict[str, int]]:
    """Extract (tool_call_count, per_tool_breakdown) from a grok session directory.

    Reads ``signals.json`` for the total ``toolCallCount`` and ``updates.jsonl``
    for per-tool breakdown (counting ``sessionUpdate == "tool_call"`` events by
    ``title``).  Falls back to ``(0, {})`` if neither file is available.
    """
    total = 0
    breakdown: dict[str, int] = {}

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
            return total, breakdown

    # Fallback: signals.json has the aggregate count but no per-tool counts.
    signals_path = session_dir / "signals.json"
    if signals_path.exists():
        try:
            signals = json.loads(signals_path.read_text(encoding="utf-8"))
            if isinstance(signals, dict):
                count = signals.get("toolCallCount")
                if isinstance(count, int) and count > 0:
                    tools_used = signals.get("toolsUsed")
                    if isinstance(tools_used, list):
                        # Without per-tool counts from updates.jsonl, distribute
                        # the total evenly as a best-effort approximation.
                        for tool_name in tools_used:
                            breakdown[str(tool_name)] = breakdown.get(str(tool_name), 0) + 1
                    return count, breakdown
        except (json.JSONDecodeError, TypeError):
            pass

    return 0, {}


class CodexSpawnToolChatAdapter:
    """Family B adapter for the ``daemon`` style — Codex via ``codex exec``.

    Spawns ``codex exec`` agentically in a neutral working directory with the
    workspace-write sandbox plus network access (so ``gcode`` can reach the
    Postgres hub), captures the final message via ``--output-last-message``,
    and reports tool-call provenance from the ``--json`` JSONL event stream.
    The target repo is byte-identical after a run because Codex is never
    spawned inside it.
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

        path = self._command_path or shutil.which("codex")
        if not path:
            raise FileNotFoundError("Codex CLI not found in PATH")
        return path

    def _build_command(
        self,
        request: ToolChatRequest,
        *,
        model: str | None,
        output_path: Path,
    ) -> list[str]:
        command = [
            self._resolve_command_path(),
            "--ask-for-approval",
            "never",
            "exec",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=true",
            "--json",
            "--output-last-message",
            str(output_path),
        ]
        if model:
            command.extend(["--model", model])
        _extend_reasoning_args(command, "codex", request.reasoning_effort)
        command.append(compose_gcode_direct_prompt(request))
        return command

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        validate_policy(request.tool_policy)
        model = request.model or next(iter(binding.models), None)
        with tempfile.TemporaryDirectory(prefix="tool-chat-codex-") as work_str:
            work = Path(work_str)
            output_path = work / "last-message.txt"
            command = self._build_command(request, model=model, output_path=output_path)
            # gcode (the read-only investigation surface) is installed in the
            # managed ~/.gobby/bin; merge_spawn_path puts it on the sandbox PATH.
            env = {"PATH": merge_spawn_path(None)}
            stdout = await _run_cli_text_generation_command(
                "Codex tool_chat",
                command,
                neutral_cwd=work,
                timeout_seconds=self._timeout_seconds,
                env_overrides=env,
            )
            file_text = (
                output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
            )
            stream_text, tool_use_count, tools = parse_codex_stream(stdout)
        text = file_text or stream_text
        if not text:
            raise RuntimeError(
                "Codex tool_chat produced no final message "
                f"(model={model}, tool_use_count={tool_use_count})"
            )
        return ToolChatResult(
            text=text,
            provider=binding.provider,
            model=model,
            tool_use_count=tool_use_count,
            turns=tool_use_count,
            tools=tools,
            applied_reasoning_effort=request.reasoning_effort,
            stop_reason="completed",
        )


def parse_droid_stream(stdout: str) -> tuple[str, int, dict[str, int]]:
    """Parse droid ``--output-format stream-json`` into (final_text, tool_calls, breakdown).

    The narrative is the ``completion`` event's ``finalText`` (falling back to the
    last assistant ``message``). Tool-call provenance comes from ``tool_call``
    events, counted by ``toolName``.
    """
    final_text = ""
    last_assistant = ""
    breakdown: dict[str, int] = {}
    total = 0
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "tool_call":
            name = str(event.get("toolName") or event.get("toolId") or "tool")
            breakdown[name] = breakdown.get(name, 0) + 1
            total += 1
        elif etype == "message" and event.get("role") == "assistant":
            text = event.get("text")
            if isinstance(text, str) and text.strip():
                last_assistant = text
        elif etype == "completion":
            final = event.get("finalText")
            if isinstance(final, str):
                final_text = final
    return (final_text or last_assistant).strip(), total, breakdown


class DroidSpawnToolChatAdapter:
    """Family B adapter for the ``cli`` style — Droid via ``droid exec``.

    Spawns ``droid exec --output-format stream-json`` in an isolated, neutral
    working directory (its Factory home seeded from the real one, so auth
    travels) with mutating/shell-file tools disabled. Execute is NOT disabled —
    the agent needs shell access to run ``gcode``. Droid's default read-only
    ``exec`` autonomy is the OS sandbox that prevents file writes. Tool-call
    provenance is read from the stream-json ``tool_call`` events.
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

        path = self._command_path or shutil.which("droid")
        if not path:
            raise FileNotFoundError("Droid CLI not found in PATH")
        return path

    def _build_command(self, request: ToolChatRequest, *, model: str | None) -> list[str]:
        command = [
            self._resolve_command_path(),
            "exec",
            # `--auto high` lets droid's Execute tool run the gcode binary; droid
            # gates arbitrary external-command exec at the high autonomy tier.
            # Truly destructive ops stay blocked without --skip-permissions-unsafe.
            "--auto",
            "high",
            "--output-format",
            "stream-json",
            "--disabled-tools",
            ",".join(_DROID_DISABLED_TOOLS),
        ]
        if model:
            command.extend(["--model", model])
        _extend_reasoning_args(command, "droid", request.reasoning_effort)
        command.append(compose_gcode_direct_prompt(request))
        return command

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        validate_policy(request.tool_policy)
        model = request.model or next(iter(binding.models), None)
        command = self._build_command(request, model=model)
        with tempfile.TemporaryDirectory(prefix="tool-chat-droid-") as work_str:
            work = Path(work_str)
            temp_home = work / "home"
            temp_home.mkdir(parents=True, exist_ok=True)
            base_env = os.environ.copy()
            _seed_droid_factory_state(base_env, temp_home)
            isolated_env = _droid_isolated_env(base_env, temp_home)
            isolated_env["PATH"] = merge_spawn_path(isolated_env.get("PATH"))
            stdout = await _run_cli_text_generation_command(
                "Droid tool_chat",
                command,
                neutral_cwd=work,
                timeout_seconds=self._timeout_seconds,
                env_overrides=isolated_env,
            )
        text, tool_use_count, tools = parse_droid_stream(stdout)
        if not text:
            raise RuntimeError(
                "Droid tool_chat produced no final message "
                f"(model={model}, tool_use_count={tool_use_count})"
            )
        return ToolChatResult(
            text=text,
            provider=binding.provider,
            model=model,
            tool_use_count=tool_use_count,
            turns=tool_use_count,
            tools=tools,
            applied_reasoning_effort=request.reasoning_effort,
            stop_reason="completed",
        )


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
            "--max-turns",
            str(request.max_turns or 30),
        ]
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
                env_overrides={"PATH": merge_spawn_path(None)},
            )
        text = ""
        session_id = ""
        try:
            result = json.loads(stdout)
            if isinstance(result, dict):
                text = (result.get("text") or "").strip()
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
        if session_id:
            session_dir = _resolve_grok_session_dir(session_id, work)
            if session_dir is not None:
                tool_use_count, tools = parse_grok_session_signals(session_dir)
        return ToolChatResult(
            text=text,
            provider=binding.provider,
            model=model,
            tool_use_count=tool_use_count,
            turns=tool_use_count,
            tools=tools,
            applied_reasoning_effort=request.reasoning_effort,
            stop_reason="completed",
        )


class QwenSpawnToolChatAdapter:
    """Family B adapter for the ``acp`` style — Qwen via headless ``qwen``.

    Spawns ``qwen`` in headless mode (positional prompt, one-shot) with the
    Seatbelt sandbox (``--sandbox``), ``--approval-mode yolo`` for unattended
    tool execution, and ``--output-format stream-json`` for tool-call
    provenance. Auth is supplied via ``--auth-type openai --openai-base-url``
    from the daemon's configured local endpoints (same pattern as the
    text_generate Qwen adapter). The agent runs ``gcode`` via shell in the
    sandbox.

    A custom seatbelt profile (``gobby-open``) is written into the neutral temp
    working directory's ``.qwen/`` folder before spawning.  This works around a
    path-resolution bug in qwen-code 0.19.x where builtin profile names resolve
    the ``.sb`` file relative to the bundled chunk directory instead of the
    package root.  Custom profile names use a cwd-relative fallback path that we
    can satisfy.
    """

    def __init__(
        self,
        *,
        command_path: str | None = None,
        timeout_seconds: float = _DEFAULT_SPAWN_TIMEOUT_SECONDS,
        openai_endpoints: Mapping[str, Any] | None = None,
    ) -> None:
        self._command_path = command_path
        self._timeout_seconds = timeout_seconds
        self._openai_endpoints = _normalize_qwen_openai_endpoints(openai_endpoints or {})

    def _select_endpoint(self, model: str | None) -> Any | None:
        if not self._openai_endpoints:
            return None
        if model:
            for endpoint in self._openai_endpoints.values():
                if endpoint.model == model:
                    return endpoint
        if len(self._openai_endpoints) == 1:
            return next(iter(self._openai_endpoints.values()))
        return None

    def _build_command(self, request: ToolChatRequest, *, model: str | None) -> list[str]:
        import shutil

        path = self._command_path or shutil.which("qwen")
        if not path:
            raise FileNotFoundError("Qwen CLI not found in PATH")
        command = [
            path,
            "--bare",
            "--sandbox",
            "--approval-mode",
            "yolo",
            "--output-format",
            "stream-json",
            "--max-tool-calls",
            str(request.limits.max_tool_calls),
            "--max-wall-time",
            f"{int(self._timeout_seconds)}s",
        ]
        endpoint = self._select_endpoint(model)
        if endpoint is not None:
            command.extend(["--auth-type", "openai", "--openai-base-url", endpoint.api_base])
            model = endpoint.model or model
        if model:
            command.extend(["--model", model])
        _extend_reasoning_args(command, "qwen", request.reasoning_effort)
        command.append(compose_gcode_direct_prompt(request))
        return command

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        validate_policy(request.tool_policy)
        model = request.model or next(iter(binding.models), None)
        command = self._build_command(request, model=model)
        env: dict[str, str] = {
            "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
            "SEATBELT_PROFILE": _QWEN_SEATBELT_PROFILE_NAME,
            "PATH": merge_spawn_path(None),
        }
        endpoint = self._select_endpoint(model)
        if endpoint is not None:
            env["OPENAI_API_KEY"] = endpoint.api_key or "not-needed"
            env["OPENAI_BASE_URL"] = endpoint.api_base
            env["OPENAI_MODEL"] = endpoint.model
        with tempfile.TemporaryDirectory(prefix="tool-chat-qwen-") as work_str:
            work = Path(work_str)
            _prepare_qwen_sandbox_profile(work)
            stdout = await _run_cli_text_generation_command(
                "Qwen tool_chat",
                command,
                neutral_cwd=work,
                timeout_seconds=self._timeout_seconds,
                env_overrides=env,
            )
        text, tool_use_count, tools = parse_qwen_stream(stdout)
        if not text:
            raise RuntimeError(
                "Qwen tool_chat produced no final message "
                f"(model={model}, tool_use_count={tool_use_count})"
            )
        return ToolChatResult(
            text=text,
            provider=binding.provider,
            model=model,
            tool_use_count=tool_use_count,
            turns=tool_use_count,
            tools=tools,
            applied_reasoning_effort=request.reasoning_effort,
            stop_reason="completed",
        )


class ACPSpawnToolChatAdapter:
    """Composite Family B adapter for the ``acp`` style (Grok + Qwen).

    The tool_chat service dispatches on ``AIAdapterStyle``, and both Grok and
    Qwen map to ``ACP``. This composite holds both sub-adapters and dispatches
    to the correct one based on ``binding.provider`` inside ``chat()``.
    """

    def __init__(self, config: DaemonConfig) -> None:
        timeout = config.ai.generation.timeout_seconds
        self._grok = GrokSpawnToolChatAdapter(timeout_seconds=timeout)
        self._qwen = QwenSpawnToolChatAdapter(
            timeout_seconds=timeout,
            openai_endpoints=config.ai.generation.local.endpoints,
        )

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        if binding.provider == "grok":
            return await self._grok.chat(request, binding)
        if binding.provider == "qwen":
            return await self._qwen.chat(request, binding)
        raise ValueError(
            f"No ACP tool_chat adapter for provider {binding.provider!r}; "
            "expected 'grok' or 'qwen'."
        )
