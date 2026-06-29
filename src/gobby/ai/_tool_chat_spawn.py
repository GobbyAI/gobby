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

import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.ai._text_generation_adapters import (
    _extend_reasoning_args,
    _run_cli_text_generation_command,
)
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolChatResult
from gobby.ai._tool_chat_tools import validate_policy

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
