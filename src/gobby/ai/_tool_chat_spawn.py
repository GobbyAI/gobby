"""Family B *spawn* adapters for the daemon ``tool_chat`` capability.

These adapters run an EXTERNAL agent CLI (Codex today; Droid/ACP to follow) in
its own agentic loop, hand it a read-only ``gcode``/``gwiki`` shim scoped to the
caller's :class:`ToolPolicy`, and capture the agent's final message as the
grounded narrative. The daemon never runs the agent's loop — that is the
difference from the Family A ``openai_compatible`` loop and the Family B
``llm_provider`` (Claude Agent SDK) adapter.

Read-only is enforced two independent ways:

* the agent runs in a neutral temp working directory under a write-confined
  sandbox, so the *target repo* is byte-identical after a run (the agent is
  never spawned inside it — ``gcode`` reads the Postgres index, not the working
  tree, so a project path is all it needs); and
* the on-``PATH`` shim only forwards the policy's whitelisted (read-only)
  subcommands and injects ``--project``, rejecting everything else — mutators
  included — and execs the real binary by absolute path so it cannot recurse.

Dispatch stays purely on :class:`AIAdapterStyle`; provider names live only in
the concrete adapter classes here and in the builder factory map, never in the
service, route, or call-site.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.ai._text_generation_adapters import (
    _droid_isolated_env,
    _extend_reasoning_args,
    _run_cli_text_generation_command,
    _seed_droid_factory_state,
)
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolChatResult
from gobby.ai._tool_chat_tools import tool_name_for, validate_policy

if TYPE_CHECKING:
    from gobby.ai._tool_chat_contracts import ToolPolicy
    from gobby.ai.registry import CapabilityBinding

_DEFAULT_SPAWN_TIMEOUT_SECONDS = 300.0


def _resolve_real_cli(cli: str) -> str:
    """Resolve the absolute path to the real ``cli`` binary (never the shim).

    Resolution happens before the shim directory is placed on ``PATH``, so a
    plain ``shutil.which`` finds the genuine binary. ``~/.gobby/bin`` is the
    install location and a reliable fallback when the daemon's ``PATH`` is
    minimal.
    """
    found = shutil.which(cli)
    if found:
        return found
    fallback = Path.home() / ".gobby" / "bin" / cli
    if fallback.exists():
        return str(fallback)
    raise FileNotFoundError(f"{cli} CLI not found in PATH or ~/.gobby/bin")


def build_readonly_cli_shim(
    policy: ToolPolicy,
    project_path: str,
    *,
    shim_dir: Path,
    log_path: Path,
) -> Path:
    """Write an executable read-only ``<cli>`` shim into ``shim_dir``.

    The shim enforces ``policy`` for a spawned agent that has shell access: it
    permits only the policy's whitelisted subcommands, injects ``--project``
    (unless the caller already passed one), appends each invocation to
    ``log_path`` for tool-call accounting, and execs the real binary by absolute
    path. ``validate_policy`` (run by the adapter before this) guarantees the
    subcommands are read-only and free of shell metacharacters.
    """
    real_binary = _resolve_real_cli(policy.cli)
    allowed = " " + " ".join(policy.tools) + " "
    shim_path = shim_dir / policy.cli
    script = f"""#!/bin/sh
# Read-only {policy.cli} shim generated for a daemon tool_chat spawn. Forwards
# only the caller's whitelisted read-only subcommands and injects --project.
REAL='{real_binary}'
PROJECT='{project_path}'
LOG='{log_path}'
ALLOWED='{allowed}'
SUB="$1"
if [ -z "$SUB" ]; then
  echo '{policy.cli}: missing subcommand (read-only tool policy)' >&2
  exit 2
fi
case "$ALLOWED" in
  *" $SUB "*) ;;
  *)
    echo "{policy.cli}: subcommand '$SUB' is not permitted by this read-only tool policy" >&2
    exit 2
    ;;
esac
shift
printf '%s\\n' "$SUB" >> "$LOG"
inject=1
for a in "$@"; do
  if [ "x$a" = 'x--project' ]; then
    inject=0
    break
  fi
done
if [ "$inject" = '1' ]; then
  exec "$REAL" "$SUB" --project "$PROJECT" "$@"
fi
exec "$REAL" "$SUB" "$@"
"""
    shim_path.write_text(script, encoding="utf-8")
    shim_path.chmod(0o755)
    return shim_path


def compose_spawn_prompt(request: ToolChatRequest) -> str:
    """Compose the seed prompt plus a read-only tool-usage preamble.

    The spawned agent investigates via shell, so — unlike the structured Family
    A/B tool surfaces — it learns the available read-only subcommands from the
    prompt. The shim enforces the same whitelist regardless of what the prompt
    says.
    """
    cli = request.tool_policy.cli
    subcommands = ", ".join(request.tool_policy.tools)
    preamble = (
        f"You are investigating an indexed codebase via the read-only `{cli}` "
        f"command, which is pre-scoped to the target project — do NOT pass "
        f"`--project`. Available read-only subcommands: {subcommands}. Use them "
        f"to gather evidence before writing, and ground every claim in "
        f"`file:line` citations from the index. Output ONLY the finished "
        f"documentation, with no preamble or tool transcripts."
    )
    parts = [part for part in (request.system_prompt, request.prompt, preamble) if part]
    return "\n\n".join(parts)


def count_tool_calls(log_path: Path) -> tuple[int, dict[str, int]]:
    """Return ``(total, per-subcommand)`` tool-call counts from the shim log."""
    if not log_path.exists():
        return 0, {}
    breakdown: dict[str, int] = {}
    total = 0
    for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        sub = raw.strip()
        if not sub:
            continue
        total += 1
        breakdown[sub] = breakdown.get(sub, 0) + 1
    return total, breakdown


class CodexSpawnToolChatAdapter:
    """Family B adapter for the ``daemon`` style — Codex via ``codex exec``.

    Spawns ``codex exec`` agentically in a neutral working directory with the
    workspace-write sandbox plus network access (so the on-``PATH`` ``gcode``
    shim can reach the Postgres hub), captures the final message via
    ``--output-last-message``, and reports tool-call provenance from the shim
    log. The target repo is byte-identical after a run because Codex is never
    spawned inside it; the shim blocks every non-whitelisted/mutating call.
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
            "--output-last-message",
            str(output_path),
        ]
        if model:
            command.extend(["--model", model])
        _extend_reasoning_args(command, "codex", request.reasoning_effort)
        command.append(compose_spawn_prompt(request))
        return command

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        validate_policy(request.tool_policy)
        model = request.model or next(iter(binding.models), None)
        with tempfile.TemporaryDirectory(prefix="tool-chat-codex-") as work_str:
            work = Path(work_str)
            shim_dir = work / "shim"
            shim_dir.mkdir()
            log_path = work / "tool-calls.log"
            log_path.write_text("", encoding="utf-8")
            build_readonly_cli_shim(
                request.tool_policy,
                request.project_path,
                shim_dir=shim_dir,
                log_path=log_path,
            )
            output_path = work / "last-message.txt"
            command = self._build_command(request, model=model, output_path=output_path)
            env = {"PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
            await _run_cli_text_generation_command(
                "Codex tool_chat",
                command,
                neutral_cwd=work,
                timeout_seconds=self._timeout_seconds,
                env_overrides=env,
            )
            text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
            tool_use_count, tools = count_tool_calls(log_path)
        if not text:
            # Hard-fail on an empty narrative rather than returning a silent
            # blank "completed" result — the caller (e.g. codewiki) must surface
            # a distinct failure, never a skeleton. No fallback.
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


# Droid tools removed so a no-OS-sandbox agent cannot mutate or escape to the
# shell: file writes, patches, the shell, automations/crons, and sub-agent
# spawning. Investigation flows ONLY through the read-only ``gobby-index`` MCP
# server (``gobby___*`` tools), which droid already connects to. Note: droid's
# default ``exec`` autonomy is itself read-only (writes are refused), so this is
# defense-in-depth on top of that guarantee, not the sole control.
_DROID_DISABLED_TOOLS: tuple[str, ...] = (
    "Execute",
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


def compose_index_investigation_prompt(request: ToolChatRequest) -> str:
    """Compose the seed prompt plus a read-only ``gobby-index`` usage preamble.

    Spawn agents that have no in-process tool surface (droid, grok, qwen)
    investigate the indexed codebase through the one gobby MCP server they
    already connect to, reaching the read-only ``gobby-index`` tools via
    progressive discovery + ``call_tool`` — never the shell or direct file I/O.
    """
    cli = request.tool_policy.cli
    tools = ", ".join(tool_name_for(cli, sub) for sub in request.tool_policy.tools)
    project = request.project_path
    preamble = (
        "Investigate the indexed codebase ONLY through the gobby MCP server named "
        '`gobby-index` (read-only). Discover its tools with list_tools("gobby-index"), '
        'then call them: call_tool("gobby-index", "<tool>", '
        f'{{"args": [...], "project": "{project}"}}). Always pass project="{project}". '
        f"Useful tools: {tools}. Do NOT use the shell, do NOT read or write files "
        "directly, do NOT modify anything. Ground every claim in `file:line` "
        "citations from the index. Output ONLY the finished documentation, with no "
        "preamble or tool transcripts."
    )
    parts = [part for part in (request.system_prompt, request.prompt, preamble) if part]
    return "\n\n".join(parts)


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
    working directory (its Factory home seeded from the real one, so auth + the
    gobby ``mcp.json`` travel) with mutating/shell tools disabled. Droid runs its
    own agentic loop, investigates the index through the read-only ``gobby-index``
    MCP server, and emits the final narrative; tool-call provenance is read from
    the stream-json ``tool_call`` events. Read-only rests on droid's default
    read-only ``exec`` autonomy plus ``gobby-index`` exposing only read-only
    gcode subcommands — droid has no OS write-sandbox of its own.
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
        path = self._command_path or shutil.which("droid")
        if not path:
            raise FileNotFoundError("Droid CLI not found in PATH")
        return path

    def _build_command(self, request: ToolChatRequest, *, model: str | None) -> list[str]:
        command = [
            self._resolve_command_path(),
            "exec",
            "--output-format",
            "stream-json",
            "--disabled-tools",
            ",".join(_DROID_DISABLED_TOOLS),
        ]
        if model:
            command.extend(["--model", model])
        _extend_reasoning_args(command, "droid", request.reasoning_effort)
        command.append(compose_index_investigation_prompt(request))
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
            stdout = await _run_cli_text_generation_command(
                "Droid tool_chat",
                command,
                neutral_cwd=work,
                timeout_seconds=self._timeout_seconds,
                env_overrides=isolated_env,
            )
        text, tool_use_count, tools = parse_droid_stream(stdout)
        if not text:
            # No silent blank "completed" result — hard-fail so the caller
            # surfaces a distinct failure rather than writing a skeleton page.
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
