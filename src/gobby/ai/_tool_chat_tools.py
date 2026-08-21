"""Repo-investigation tool registry for the ``tool_chat`` feature.

Provider-agnostic and caller-parameterized. A :class:`ToolPolicy` declares the
executable family (``gcode``/``gwiki``), the exposed subcommands, and whether
mutation is permitted. This module:

* validates a policy against the per-CLI command allowlist and read whitelist —
  every allowed subcommand that is not on the read whitelist is treated as
  mutating (default-deny) and is rejected unless the policy sets
  ``allow_mutation``;
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
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from gobby.agents.sandbox_policy import canonical_path, sensitive_roots
from gobby.ai._tool_chat_builtins import (
    BuiltinExecutionContext,
    BuiltinToolResult,
    BuiltinToolSpec,
    InvocationRecord,
    new_evidence_ref,
    serialize_builtin_tool_result,
    success_payload_capacity,
    tool_result_too_large,
    validate_builtin_arguments,
    validate_builtin_spec,
)
from gobby.ai._tool_chat_contracts import ToolLoopLimits, ToolPolicy
from gobby.storage.managed_credentials import MANAGED_EXECUTION_BOOTSTRAP_ENV

_OPERATOR_DATABASE_ENV = ("DATABASE_URL", "GCODE_DATABASE_URL", "GOBBY_POSTGRES_DSN")

logger = logging.getLogger(__name__)

# Read-only subcommands per CLI. Allowed commands outside this set are treated
# as mutating (default-deny) and require ``ToolPolicy.allow_mutation``.
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
        "callees",
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
# Nested argv under a mutating parent. `graph` stays off the read whitelist;
# a no-mutation policy may still expose it and authorize exactly `graph view`.
_GCODE_READONLY_NESTED: dict[str, frozenset[str]] = {
    "graph": frozenset({"view"}),
}
GCODE_ALLOWED_TOOLS: frozenset[str] = GCODE_READONLY_TOOLS | frozenset(
    {
        "init",
        "setup",
        "index",
        "invalidate",
        "graph",
        "vector",
        "embeddings",
        "prune",
    }
)
GWIKI_ALLOWED_TOOLS: frozenset[str] = GWIKI_READONLY_TOOLS | frozenset({"compile"})
_ALLOWED_BY_CLI: dict[str, frozenset[str]] = {
    "gcode": GCODE_ALLOWED_TOOLS,
    "gwiki": GWIKI_ALLOWED_TOOLS,
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
    ("gcode", "callees"): ('Who a function/method calls. args: ["<symbol-id-or-name>"].'),
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


def tool_result_is_error(text: str) -> bool:
    """Classify text and typed JSON tool failures consistently across adapters."""
    if text.lstrip().lower().startswith("[error"):
        return True
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict) and (
        payload.get("success") is False
        or payload.get("ok") is False
        or (isinstance(payload.get("error_code"), str) and bool(payload.get("error_code")))
    )


def _is_readonly(cli: str, subcommand: str) -> bool:
    return subcommand in _READONLY_BY_CLI.get(cli, frozenset())


def _nested_readonly_parent(cli: str, subcommand: str) -> bool:
    return cli == "gcode" and subcommand in _GCODE_READONLY_NESTED


def _is_readonly_invocation(cli: str, subcommand: str, args: list[str]) -> bool:
    if _is_readonly(cli, subcommand):
        return True
    if not _nested_readonly_parent(cli, subcommand):
        return False
    return bool(args) and args[0] in _GCODE_READONLY_NESTED[subcommand]


def tool_name_for(cli: str, subcommand: str) -> str:
    """Return the stable OpenAI/MCP tool name for a CLI subcommand."""
    return f"{cli}_{subcommand.replace('-', '_')}"


def validate_policy(policy: ToolPolicy, *, allow_empty: bool = False) -> None:
    """Validate ``policy`` against the registry whitelist.

    Raises :class:`ToolPolicyError` for an unknown CLI, an empty tool set unless
    ``allow_empty`` is true, a malformed subcommand token, any subcommand outside
    the CLI allowlist, or — when ``allow_mutation`` is False — any subcommand
    that is not on the read whitelist.
    """
    if policy.cli not in _READONLY_BY_CLI:
        raise ToolPolicyError(
            f"Unknown tool CLI {policy.cli!r}; expected one of {sorted(_READONLY_BY_CLI)}."
        )
    if not policy.tools and not allow_empty:
        raise ToolPolicyError("Tool policy must expose at least one subcommand.")
    for subcommand in policy.tools:
        if not subcommand or any(ch in subcommand for ch in _SHELL_METACHARACTERS):
            raise ToolPolicyError(f"Malformed subcommand token {subcommand!r}.")
        if subcommand not in _ALLOWED_BY_CLI[policy.cli]:
            raise ToolPolicyError(
                f"Subcommand {policy.cli} {subcommand!r} is not allowed by policy."
            )
        if not policy.allow_mutation and not (
            _is_readonly(policy.cli, subcommand) or _nested_readonly_parent(policy.cli, subcommand)
        ):
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


def _decode_utf8_view(value: bytes) -> tuple[str, int]:
    """Decode a byte window after dropping incomplete UTF-8 boundary bytes."""

    text = value.decode("utf-8", errors="ignore")
    return text, len(text.encode("utf-8"))


def _cap_text(value: str, byte_cap: int) -> str:
    """Return a UTF-8-safe view whose complete text fits the shared cap."""
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_cap:
        return value
    return encoded[:byte_cap].decode("utf-8", errors="ignore")


async def run_argv(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    byte_cap: int,
    env: dict[str, str] | None = None,
) -> str:
    """Run ``argv`` as a subprocess in ``cwd`` and return capped combined output.

    Uses ``create_subprocess_exec`` (no shell). On a nonzero exit the captured
    stderr tail is appended so the model sees the failure. A timeout or a
    missing executable returns an explanatory note rather than raising, so the
    tool loop can continue.
    """
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        return _cap_text(
            f"[error: working directory not found or not a directory: {cwd!r}]",
            byte_cap,
        )
    try:
        if env is None:
            child_env = None
        else:
            child_env = {**os.environ, **env}
            if MANAGED_EXECUTION_BOOTSTRAP_ENV in env:
                for name in _OPERATOR_DATABASE_ENV:
                    child_env.pop(name, None)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
    except FileNotFoundError:
        return _cap_text(f"[error: executable {argv[0]!r} not found]", byte_cap)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return _cap_text(f"[error: tool timed out after {timeout:g}s]", byte_cap)

    out = stdout or b""
    shown = out[:byte_cap]
    text, shown_bytes = _decode_utf8_view(shown)
    if len(out) > byte_cap:
        text += (
            f"\n[output truncated: first {shown_bytes} of {len(out)} bytes shown "
            f"(cap {byte_cap}). Narrow the query (gcode supports --limit/--offset) "
            "or drill down by symbol.]"
        )
    if proc.returncode != 0:
        stderr_bytes = stderr or b""
        err_tail, stderr_shown_bytes = _decode_utf8_view(stderr_bytes[-2048:])
        if len(stderr_bytes) > 2048:
            err_tail += (
                f"\n[stderr: last {stderr_shown_bytes} of {len(stderr_bytes)} bytes (cap 2048)]"
            )
        text = (
            f"{text}\n[exit {proc.returncode}: {err_tail}]"
            if text
            else f"[exit {proc.returncode}: {err_tail}]"
        )
    return _cap_text(text, byte_cap)


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
        builtins: tuple[BuiltinToolSpec, ...] = (),
        subprocess_env: dict[str, str] | None = None,
        managed_execution_id: UUID | None = None,
    ) -> None:
        validate_policy(policy, allow_empty=bool(builtins))
        self._policy = policy
        self._project_path = project_path
        self._limits = limits or ToolLoopLimits()
        self._subprocess_env = subprocess_env
        self._managed_execution_id = managed_execution_id
        self._subcommand_by_tool_name: dict[str, str] = {
            tool_name_for(policy.cli, sub): sub for sub in policy.tools
        }
        self._builtin_by_name: dict[str, BuiltinToolSpec] = {}
        for spec in builtins:
            try:
                validate_builtin_spec(spec)
            except ValueError as exc:
                raise ToolPolicyError(str(exc)) from exc
            if spec.name in self._builtin_by_name:
                raise ToolPolicyError(f"Builtin tool name {spec.name!r} is duplicated.")
            if spec.name in self._subcommand_by_tool_name:
                raise ToolPolicyError(f"Builtin tool name {spec.name!r} collides with a CLI tool.")
            self._builtin_by_name[spec.name] = spec
        self._calls_used = 0
        self.invocation_log: list[InvocationRecord] = []
        self._builtin_tasks: set[asyncio.Task[BuiltinToolResult]] = set()

    @property
    def policy(self) -> ToolPolicy:
        return self._policy

    def tool_names(self) -> tuple[str, ...]:
        return (*self._subcommand_by_tool_name, *self._builtin_by_name)

    @property
    def calls_used(self) -> int:
        return self._calls_used

    @property
    def budget_exhausted(self) -> bool:
        return self._calls_used >= self._limits.max_tool_calls

    def input_schema_for(self, tool_name: str) -> dict[str, Any]:
        """Return the exact provider input schema for one exposed tool."""

        builtin = self._builtin_by_name.get(tool_name)
        if builtin is not None:
            return builtin.input_schema
        self.resolve(tool_name)
        return {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        f"Arguments for `{self._policy.cli} "
                        f"{self._subcommand_by_tool_name[tool_name]}` (flags and positionals)."
                    ),
                }
            },
            "required": [],
            "additionalProperties": False,
        }

    def description_for(self, tool_name: str) -> str:
        """Return the provider-facing description for one exposed tool."""

        builtin = self._builtin_by_name.get(tool_name)
        if builtin is not None:
            return builtin.description
        subcommand = self.resolve(tool_name)
        return _TOOL_DESCRIPTIONS.get(
            (self._policy.cli, subcommand),
            f"Run `{self._policy.cli} {subcommand}` (repo investigation).",
        )

    def openai_schemas(self) -> list[dict[str, Any]]:
        """Render the policy's tools as OpenAI ``tools[]`` function schemas."""
        schemas: list[dict[str, Any]] = []
        for tool_name in self.tool_names():
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": self.description_for(tool_name),
                        "parameters": self.input_schema_for(tool_name),
                    },
                }
            )
        return schemas

    def resolve(self, tool_name: str) -> str:
        """Return the subcommand for ``tool_name`` or raise if not in the policy."""
        subcommand = self._subcommand_by_tool_name.get(tool_name)
        if subcommand is None:
            raise ToolPolicyError(f"Tool {tool_name!r} is not exposed by this policy.")
        if not self._policy.allow_mutation and not (
            _is_readonly(self._policy.cli, subcommand)
            or _nested_readonly_parent(self._policy.cli, subcommand)
        ):
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
        if self.budget_exhausted:
            result = BuiltinToolResult(
                error_code="tool_call_budget_exhausted",
                error="tool call budget exhausted",
            )
            text, fitted, _ = self._fit_result(result)
            self._record(tool_name, arguments, text, result=fitted)
            return text

        self._calls_used += 1
        builtin = self._builtin_by_name.get(tool_name)
        if builtin is not None:
            return await self._execute_builtin(builtin, arguments)

        try:
            subcommand = self.resolve(tool_name)
            args = self._args_from(arguments)
            if not self._policy.allow_mutation and not _is_readonly_invocation(
                self._policy.cli, subcommand, args
            ):
                raise ToolPolicyError(
                    f"Tool {tool_name!r} argv {args!r} is not permitted by a read-only policy."
                )
        except ToolPolicyError as exc:
            self._record(
                tool_name,
                arguments,
                f"[error: {exc}]",
                result=BuiltinToolResult(error_code="tool_policy_error", error=str(exc)),
            )
            raise
        argv = [self._policy.cli, subcommand, *args]
        logger.debug(
            "tool_chat executing",
            extra={
                "cli": self._policy.cli,
                "subcommand": subcommand,
                "cwd": self._project_path,
                "arg_count": len(args),
            },
        )
        text = await run_argv(
            argv,
            cwd=self._project_path,
            timeout=self._limits.tool_timeout_seconds,
            byte_cap=self._limits.max_bytes_per_tool_result,
            env=self._subprocess_env,
        )
        self._record(tool_name, arguments, text)
        return text

    async def _execute_builtin(self, spec: BuiltinToolSpec, arguments: object) -> str:
        errors = validate_builtin_arguments(arguments, spec.input_schema)
        if errors or not isinstance(arguments, dict):
            result = BuiltinToolResult(
                error_code="invalid_tool_arguments",
                error="builtin arguments failed schema validation",
                details={"errors": errors},
            )
            text, fitted, _ = self._fit_result(result)
            self._record(spec.name, arguments, text, result=fitted)
            return text

        if self._managed_execution_id is not None and self._has_sensitive_path(spec, arguments):
            result = BuiltinToolResult(
                error_code="sensitive_path_denied",
                error="managed builtin path is sensitive",
            )
            text, fitted, _ = self._fit_result(result)
            self._record(spec.name, arguments, text, result=fitted)
            return text

        evidence_ref = new_evidence_ref()
        max_payload_bytes = success_payload_capacity(
            self._limits.max_bytes_per_tool_result,
            evidence_ref,
        )
        if max_payload_bytes < 0:
            result = tool_result_too_large()
            text, fitted, _ = self._fit_result(result)
            self._record(spec.name, arguments, text, result=fitted)
            return text

        timeout = self._limits.tool_timeout_seconds
        cleanup_grace = min(5.0, timeout / 2)
        context = BuiltinExecutionContext(
            max_payload_bytes=max_payload_bytes,
            evidence_ref=evidence_ref,
            subprocess_deadline=time.monotonic() + timeout - cleanup_grace,
            managed_execution_id=self._managed_execution_id,
        )
        result = await self._await_builtin(spec, arguments, context, timeout=timeout)
        result_ref = evidence_ref if result.ok else None
        text, fitted_result, result_ref = self._fit_result(result, evidence_ref=result_ref)
        self._record(
            spec.name,
            arguments,
            text,
            result=fitted_result,
            evidence_ref=result_ref,
        )
        return text

    def _has_sensitive_path(self, spec: BuiltinToolSpec, arguments: dict[str, Any]) -> bool:
        project_root = Path(self._project_path)
        protected = tuple(Path(path) for path in sensitive_roots())
        for argument in spec.path_arguments:
            value = arguments.get(argument)
            if not isinstance(value, str):
                continue
            candidate = Path(canonical_path(value, base=project_root))
            if any(
                candidate == root or root in candidate.parents or candidate in root.parents
                for root in protected
            ):
                return True
        return False

    async def _await_builtin(
        self,
        spec: BuiltinToolSpec,
        arguments: dict[str, Any],
        context: BuiltinExecutionContext,
        *,
        timeout: float,
    ) -> BuiltinToolResult:
        task = asyncio.create_task(spec.handler(arguments, context))
        self._builtin_tasks.add(task)
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            await asyncio.gather(task, return_exceptions=True)
            return BuiltinToolResult(
                error_code="tool_timeout",
                error=f"tool timed out after {timeout:g}s",
            )
        except asyncio.CancelledError:
            if task.cancelled():
                return BuiltinToolResult(
                    error_code="tool_cancelled",
                    error="builtin handler cancelled",
                )
            await asyncio.gather(task, return_exceptions=True)
            raise
        except Exception as exc:
            return self._builtin_failure(exc)
        finally:
            if task.done():
                self._builtin_tasks.discard(task)
        if not isinstance(result, BuiltinToolResult):
            return BuiltinToolResult(
                error_code="invalid_tool_result",
                error="builtin handler returned an invalid result",
            )
        return result

    @staticmethod
    def _builtin_failure(exc: Exception) -> BuiltinToolResult:
        error_code = getattr(exc, "code", "builtin_execution_failed")
        if not isinstance(error_code, str):
            error_code = "builtin_execution_failed"
        raw_details = getattr(exc, "details", None)
        details = raw_details if isinstance(raw_details, dict) else None
        return BuiltinToolResult(
            error_code=error_code,
            error=str(exc) or type(exc).__name__,
            details=details,
        )

    def _fit_result(
        self,
        result: BuiltinToolResult,
        *,
        evidence_ref: str | None = None,
    ) -> tuple[str, BuiltinToolResult, str | None]:
        """Serialize under the byte cap, returning the result actually served."""
        fitted = result
        ref = evidence_ref
        try:
            text = serialize_builtin_tool_result(fitted, evidence_ref=ref)
        except (TypeError, ValueError):
            fitted = BuiltinToolResult(
                error_code="invalid_tool_result",
                error="builtin result is not JSON serializable",
            )
            ref = None
            text = serialize_builtin_tool_result(fitted)
        if len(text.encode("utf-8")) > self._limits.max_bytes_per_tool_result:
            fitted = tool_result_too_large()
            ref = None
            text = serialize_builtin_tool_result(fitted)
        assert len(text.encode("utf-8")) <= self._limits.max_bytes_per_tool_result
        return text, fitted, ref

    def _record(
        self,
        tool_name: str,
        arguments: object,
        text: str,
        *,
        result: BuiltinToolResult | None = None,
        evidence_ref: str | None = None,
    ) -> None:
        record: InvocationRecord = {
            "tool_name": tool_name,
            "arguments": dict(arguments) if isinstance(arguments, dict) else arguments,
            "result_size_bytes": len(text.encode("utf-8")),
            "ok": result.ok if result is not None else True,
            "error_code": result.error_code if result is not None else None,
            "evidence_ref": evidence_ref,
        }
        if result is not None:
            if result.selector is not None:
                record["selector"] = result.selector
            if result.range is not None:
                record["range"] = result.range
            if result.complete is not None:
                record["complete"] = result.complete
            if result.content_hash is not None:
                record["content_hash"] = result.content_hash
        self.invocation_log.append(record)


def cli_available(cli: str) -> bool:
    """Return whether ``cli`` is resolvable on PATH (binding availability probe)."""
    return shutil.which(cli) is not None
