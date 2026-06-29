"""Repo-investigation tool registry for the ``tool_chat`` feature.

Provider-agnostic and caller-parameterized. A :class:`ToolPolicy` declares the
executable family (``gcode``/``gwiki``), the exposed subcommands, and whether
mutation is permitted. This module:

* validates a policy against the per-CLI read whitelist — every subcommand that
  is not on the read whitelist is treated as mutating (default-deny) and is
  rejected unless the policy sets ``allow_mutation``;
* renders the policy as OpenAI function-tool schemas (Family A loop) and exposes
  a stable tool-name <-> subcommand mapping reusable by other adapter families;
* executes a validated tool call as an ``argv`` subprocess in
  ``cwd=project_path`` with a timeout and an output byte cap — **never via a
  shell**, and rejecting shell metacharacters in arguments as defense in depth.

The feature core hardcodes no tool set, prompt, or read-only law; everything is
derived from the caller's policy.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from gobby.ai._tool_chat_contracts import ToolLoopLimits, ToolPolicy

logger = logging.getLogger(__name__)

# Read-only subcommands per CLI. Anything not listed here is treated as a
# mutating subcommand (default-deny) and requires ``ToolPolicy.allow_mutation``.
GCODE_READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "search",
        "search-symbol",
        "search-text",
        "search-content",
        "grep",
        "outline",
        "symbol",
        "symbols",
        "symbol-at",
        "repo-outline",
        "tree",
        "kinds",
        "callers",
        "usages",
        "imports",
        "path",
        "blast-radius",
    }
)
GWIKI_READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "search",
        "read",
        "backlinks",
        "sources",
        "status",
        "trust",
        "audit",
        "lint",
    }
)
_READONLY_BY_CLI: dict[str, frozenset[str]] = {
    "gcode": GCODE_READONLY_TOOLS,
    "gwiki": GWIKI_READONLY_TOOLS,
}

# Characters that must never reach an argument. Execution is ``argv``-based
# (no shell), so these are inert in practice, but rejecting them keeps the
# contract explicit and blocks any attempt to express command chaining.
_SHELL_METACHARACTERS: frozenset[str] = frozenset(";&|`$<>\n\r\x00")

# Curated descriptions for the most-used investigation subcommands so the model
# uses them well. Missing entries fall back to a generic description.
_TOOL_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("gcode", "search"): (
        "Hybrid code search (BM25 + semantic + graph). Best for fuzzy concepts "
        'or natural-language queries. args: ["query", optional path filters].'
    ),
    ("gcode", "search-symbol"): (
        'Exact-first symbol lookup by name. args: ["name", optional path filters].'
    ),
    ("gcode", "search-content"): (
        "Ranked full-text search across repo text (source, comments, docs, "
        'config). args: ["query", optional path filters].'
    ),
    ("gcode", "grep"): (
        'Grep indexed content. args: regex by default; use ["-F", "literal"] '
        'for fixed strings or ["-w", "identifier"] for whole-word matches.'
    ),
    ("gcode", "outline"): (
        'Hierarchical symbol map of one file (cheaper than reading it). args: ["path/to/file"].'
    ),
    ("gcode", "symbol"): (
        'Retrieve one symbol\'s source by its full stored UUID. args: ["<uuid>"].'
    ),
    ("gcode", "symbols"): (
        'Batch-retrieve symbol sources by full stored UUIDs. args: ["<uuid>", ...].'
    ),
    ("gcode", "symbol-at"): (
        'Retrieve the symbol at a known file:line. args: ["path/to/file:42"].'
    ),
    ("gcode", "repo-outline"): (
        "High-level project summary with per-module symbol counts. args: []."
    ),
    ("gcode", "callers"): ('Who calls a function/method. args: ["<symbol-id-or-name>"].'),
    ("gcode", "usages"): (
        'All usages (calls + imports) of a symbol. args: ["<symbol-id-or-name>"].'
    ),
    ("gcode", "imports"): ('What a file imports. args: ["path/to/file"].'),
    ("gcode", "blast-radius"): ('Transitive call/import impact of a symbol. args: ["<name>"].'),
    ("gwiki", "search"): ('Hybrid vault search over ingested knowledge. args: ["query"].'),
    ("gwiki", "read"): ('Read one vault document. args: ["<doc-id-or-path>"].'),
    ("gwiki", "backlinks"): ('Documents linking to a target. args: ["<doc-id-or-path>"].'),
    ("gwiki", "sources"): ("List provenance sources for the vault. args: []."),
}


class ToolPolicyError(ValueError):
    """Raised when a policy is invalid or a tool call violates the policy."""


def _is_readonly(cli: str, subcommand: str) -> bool:
    return subcommand in _READONLY_BY_CLI.get(cli, frozenset())


def tool_name_for(cli: str, subcommand: str) -> str:
    """Return the stable OpenAI/MCP tool name for a CLI subcommand."""
    return f"{cli}_{subcommand.replace('-', '_')}"


def validate_policy(policy: ToolPolicy) -> None:
    """Validate ``policy`` against the registry whitelist.

    Raises :class:`ToolPolicyError` for an unknown CLI, an empty tool set, a
    malformed subcommand token, or — when ``allow_mutation`` is False — any
    subcommand that is not on the read whitelist.
    """
    if policy.cli not in _READONLY_BY_CLI:
        raise ToolPolicyError(
            f"Unknown tool CLI {policy.cli!r}; expected one of {sorted(_READONLY_BY_CLI)}."
        )
    if not policy.tools:
        raise ToolPolicyError("Tool policy must expose at least one subcommand.")
    for subcommand in policy.tools:
        if not subcommand or any(ch in subcommand for ch in _SHELL_METACHARACTERS):
            raise ToolPolicyError(f"Malformed subcommand token {subcommand!r}.")
        if not policy.allow_mutation and not _is_readonly(policy.cli, subcommand):
            raise ToolPolicyError(
                f"Subcommand {policy.cli} {subcommand!r} is not read-only; "
                "it requires a policy with allow_mutation=True."
            )


def _reject_metacharacters(args: list[str]) -> None:
    for arg in args:
        if not isinstance(arg, str):
            raise ToolPolicyError(f"Tool argument must be a string, got {type(arg)!r}.")
        if any(ch in arg for ch in _SHELL_METACHARACTERS):
            raise ToolPolicyError(
                f"Tool argument {arg!r} contains a disallowed shell metacharacter."
            )


async def run_argv(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    byte_cap: int,
) -> str:
    """Run ``argv`` as a subprocess in ``cwd`` and return capped combined output.

    Uses ``create_subprocess_exec`` (no shell). On a nonzero exit the captured
    stderr tail is appended so the model sees the failure. A timeout or a
    missing executable returns an explanatory note rather than raising, so the
    tool loop can continue.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return f"[error: executable {argv[0]!r} not found]"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return f"[error: tool timed out after {timeout:g}s]"

    out = stdout or b""
    truncated = len(out) > byte_cap
    text = out[:byte_cap].decode("utf-8", errors="replace")
    if truncated:
        text += "\n[output truncated]"
    if proc.returncode != 0:
        err_tail = (stderr or b"")[:2048].decode("utf-8", errors="replace").strip()
        text = (
            f"{text}\n[exit {proc.returncode}: {err_tail}]"
            if text
            else f"[exit {proc.returncode}: {err_tail}]"
        )
    return text


class ToolRuntime:
    """Validated, executable view of a :class:`ToolPolicy` for one project.

    Wraps a policy plus the project path and loop limits. Adapters use it to
    render tool schemas and to execute model-issued tool calls under the policy.
    """

    def __init__(
        self,
        policy: ToolPolicy,
        *,
        project_path: str,
        limits: ToolLoopLimits | None = None,
    ) -> None:
        validate_policy(policy)
        self._policy = policy
        self._project_path = project_path
        self._limits = limits or ToolLoopLimits()
        self._subcommand_by_tool_name: dict[str, str] = {
            tool_name_for(policy.cli, sub): sub for sub in policy.tools
        }

    @property
    def policy(self) -> ToolPolicy:
        return self._policy

    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._subcommand_by_tool_name)

    def openai_schemas(self) -> list[dict[str, Any]]:
        """Render the policy's tools as OpenAI ``tools[]`` function schemas."""
        cli = self._policy.cli
        schemas: list[dict[str, Any]] = []
        for tool_name, subcommand in self._subcommand_by_tool_name.items():
            description = _TOOL_DESCRIPTIONS.get(
                (cli, subcommand),
                f"Run `{cli} {subcommand}` (repo investigation).",
            )
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": description,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "args": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        f"Arguments for `{cli} {subcommand}` "
                                        "(flags and positionals)."
                                    ),
                                }
                            },
                            "required": [],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        return schemas

    def resolve(self, tool_name: str) -> str:
        """Return the subcommand for ``tool_name`` or raise if not in the policy."""
        subcommand = self._subcommand_by_tool_name.get(tool_name)
        if subcommand is None:
            raise ToolPolicyError(f"Tool {tool_name!r} is not exposed by this policy.")
        if not self._policy.allow_mutation and not _is_readonly(self._policy.cli, subcommand):
            raise ToolPolicyError(f"Tool {tool_name!r} is not permitted by a read-only policy.")
        return subcommand

    def _args_from(self, arguments: Any) -> list[str]:
        if arguments is None:
            return []
        if not isinstance(arguments, dict):
            raise ToolPolicyError("Tool arguments must be an object.")
        raw = arguments.get("args", [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ToolPolicyError("Tool argument 'args' must be an array of strings.")
        args = [str(item) for item in raw]
        _reject_metacharacters(args)
        return args

    async def execute(self, tool_name: str, arguments: Any) -> str:
        """Validate and execute one model-issued tool call, returning its output.

        Raises :class:`ToolPolicyError` when the call violates the policy (the
        caller surfaces that back to the model as an error result and the target
        repo is left untouched). Subprocess failures are returned as text.
        """
        subcommand = self.resolve(tool_name)
        args = self._args_from(arguments)
        argv = [self._policy.cli, subcommand, *args]
        logger.debug("tool_chat executing: %s (cwd=%s)", argv, self._project_path)
        return await run_argv(
            argv,
            cwd=self._project_path,
            timeout=self._limits.tool_timeout_seconds,
            byte_cap=self._limits.per_tool_result_byte_cap,
        )


def cli_available(cli: str) -> bool:
    """Return whether ``cli`` is resolvable on PATH (binding availability probe)."""
    return shutil.which(cli) is not None
