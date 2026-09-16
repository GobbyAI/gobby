"""Trusted, permission-neutral command transformation handlers."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from gobby.adapters.capabilities import get_provider_capabilities
from gobby.hooks.effect_deadline import (
    BLOCKING_EFFECT_BUDGET_SECONDS,
    BlockingEffectDeadline,
    blocking_budget_overrun,
    elapsed_blocking_effect_seconds,
)
from gobby.hooks.events import HookEvent, SessionSource
from gobby.integrations.rtk import resolve_rtk
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.utils.dev import linked_worktree_root
from gobby.workflows.definitions import RuleEffect
from gobby.workflows.engine._offload import offload

logger = logging.getLogger(__name__)

_DEFAULT_PROXY_TIMEOUT_SECONDS = 2.0
_MAX_PROXY_OUTPUT_BYTES = 64 * 1024
# Leading shell context RTK must carry through a rewrite: the ``NAME=value``
# words before the command (each value bare or quoted) or a leading ``cd``
# segment up to its separator.
_SHELL_CONTEXT_PREFIX = re.compile(
    r"^\s*(?:"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|[^\s;&|'\"])*[ \t]*)+"
    r"|cd(?:[ \t][^;&|]*|$)"
    r")"
)
_RTK_DIAGNOSTIC_PREFIX = re.compile(r"^\s*(?:\[rtk\s*:|rtk(?:\s+error)?\s*:)", re.IGNORECASE)
_RTK_UNSUPPORTED_JQ_REWRITE = re.compile(r"(?:^|\s)rtk\s+(?:\S*/)?jq(?:\s|$)")
_RTK_GIT_REWRITE = re.compile(r"(?:^|[\s;&|(])rtk\s+(?:\S*/)?git(?:\s|$)")

# One WARNING per unavailability episode; DEBUG until RTK resolves again.
_rtk_unavailable_warned = False


def _shell_context(command: str) -> str:
    """Return the leading assignments or ``cd`` segment a rewrite must keep."""
    match = _SHELL_CONTEXT_PREFIX.match(command)
    return match.group(0).strip() if match else ""


def _detaches_shell_context(command: str, transformed: str) -> bool:
    """Report a rewrite that moved the launcher in front of the shell context.

    ``DATABASE_URL=... uv run pytest`` must come back as ``DATABASE_URL=... uv run
    rtk pytest``; a rewrite that reorders or drops the assignments or the leading
    ``cd`` would run the command in a different environment or directory.
    """
    return _shell_context(transformed) != _shell_context(command)


def _is_plausible_rewrite(command: str) -> bool:
    """Reject RTK diagnostics and bytes that cannot form a safe shell command."""
    if not command or _RTK_DIAGNOSTIC_PREFIX.match(command):
        return False
    # RTK 0.48.0 can emit ``rtk jq`` (and ``rtk /usr/bin/jq``), but it has no
    # jq subcommand. Preserve the original command instead of accepting a
    # rewrite that is guaranteed to fail before the requested validation runs.
    if _RTK_UNSUPPORTED_JQ_REWRITE.search(command):
        return False
    return not any(ord(char) < 32 and char not in "\t\n\r" for char in command)


def _is_refused_worktree_git_rewrite(event: HookEvent, transformed: str) -> bool:
    """Report a rewrite that Claude Code's worktree containment would refuse.

    Claude Code judges git commands in a linked worktree by what they run, and it
    cannot see through the RTK launcher, so ``rtk git`` there is refused even when
    the bare ``git`` it wraps would run.
    """
    if event.source is not SessionSource.CLAUDE or not event.cwd:
        return False
    return (
        _RTK_GIT_REWRITE.search(transformed) is not None
        and linked_worktree_root(Path(event.cwd)) is not None
    )


def _note_rtk_unavailable(rule_name: str) -> None:
    global _rtk_unavailable_warned
    level = logging.DEBUG if _rtk_unavailable_warned else logging.WARNING
    _rtk_unavailable_warned = True
    logger.log(level, "proxy_hook[%s]: compatible RTK executable unavailable", rule_name)


def _note_rtk_available() -> None:
    global _rtk_unavailable_warned
    _rtk_unavailable_warned = False


class _OutputTooLarge(Exception):
    pass


@dataclass(frozen=True)
class ProxyHookInvocation:
    """One matched proxy effect, retained in rule-priority order."""

    effect: RuleEffect
    row: RuleDefinitionRow


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    *,
    limit: int,
) -> bytes:
    if stream is None:
        return b""
    output = bytearray()
    while True:
        chunk = await stream.read(min(8192, limit + 1 - len(output)))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > limit:
            raise _OutputTooLarge


async def _collect_process_output(
    process: asyncio.subprocess.Process,
) -> tuple[int, bytes, bytes]:
    tasks = (
        asyncio.create_task(_read_bounded(process.stdout, limit=_MAX_PROXY_OUTPUT_BYTES)),
        asyncio.create_task(_read_bounded(process.stderr, limit=_MAX_PROXY_OUTPUT_BYTES)),
        asyncio.create_task(process.wait()),
    )
    try:
        stdout, stderr, code = await asyncio.gather(*tasks)
        return code, stdout, stderr
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=0.5)
    except TimeoutError:
        logger.warning("proxy_hook: failed to reap terminated handler process")


def _warn_budget_overrun(
    rule_name: str,
    deadline: BlockingEffectDeadline | None,
    *,
    stage: str,
    floor: float,
) -> None:
    """Report a spent shared budget so the log alone identifies the cause."""
    logger.warning(
        "proxy_hook[%s]: blocking budget spent %s, running on the %.1fs floor "
        "(shared budget %.1fs of %.1fs spent)",
        rule_name,
        stage,
        floor,
        elapsed_blocking_effect_seconds(deadline),
        BLOCKING_EFFECT_BUDGET_SECONDS,
    )


class ProxyHooksMixin:
    """Execute an internal registry of trusted command transformers."""

    async def _run_proxy_hooks(
        self,
        invocations: list[ProxyHookInvocation],
        event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None,
    ) -> bool:
        try:
            capabilities = get_provider_capabilities(event.source)
        except KeyError:
            logger.info("proxy_hook: provider %s has no adapter capabilities", event.source.value)
            return False
        if not capabilities.supports_permission_neutral_rewrite:
            logger.info(
                "proxy_hook: provider %s cannot rewrite input without changing permission",
                event.source.value,
            )
            return False

        changed = False
        for invocation in invocations:
            handler = invocation.effect.handler
            if handler != "rtk":
                logger.warning(
                    "proxy_hook[%s]: unknown trusted handler %r",
                    invocation.row.name,
                    handler,
                )
                continue
            changed = (
                await self._run_rtk_proxy(
                    invocation,
                    event,
                    blocking_deadline=blocking_deadline,
                )
                or changed
            )
        return changed

    async def _run_rtk_proxy(
        self,
        invocation: ProxyHookInvocation,
        event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None,
    ) -> bool:
        tool_input = event.data.get("tool_input")
        if not isinstance(tool_input, dict):
            return False
        command = tool_input.get("command")
        if not isinstance(command, str):
            return False
        # This stage runs last (``core.py`` defers proxy transformations until
        # every original-input denial has passed), so a shared budget with a
        # zero floor would starve it alone and drop the rewrite exactly under
        # the load that makes rewriting matter. Its declared timeout is a
        # reservation here, and an overrun is reported rather than obeyed.
        timeout = invocation.effect.timeout_seconds or _DEFAULT_PROXY_TIMEOUT_SECONDS
        overran = blocking_budget_overrun(blocking_deadline)
        if overran:
            _warn_budget_overrun(
                invocation.row.name,
                blocking_deadline,
                stage="before the RTK probe",
                floor=timeout,
            )

        probe_timeout = min(0.5, max(timeout / 4, 0.05))
        probe = await offload(resolve_rtk, timeout=probe_timeout)
        if probe is None:
            _note_rtk_unavailable(invocation.row.name)
            return False
        _note_rtk_available()

        # One warning per event: the budget stays spent once it is spent, so
        # only the stage that first observed the overrun reports it.
        if not overran and blocking_budget_overrun(blocking_deadline):
            _warn_budget_overrun(
                invocation.row.name,
                blocking_deadline,
                stage="after the RTK probe",
                floor=timeout,
            )

        # ``rewrite`` is the contract stock RTK host hooks use, so its heredoc,
        # substitution, and redirect gates apply. ``--`` keeps a command that
        # starts with a hyphen from being parsed as a flag.
        argv = [str(probe.path), "rewrite", "--", command]
        cwd = event.cwd if event.cwd else None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            logger.warning("proxy_hook[%s]: RTK spawn failed: %s", invocation.row.name, exc)
            return False

        try:
            code, stdout, stderr = await asyncio.wait_for(
                _collect_process_output(process),
                timeout=timeout,
            )
        except TimeoutError:
            await _terminate_process(process)
            logger.warning("proxy_hook[%s]: RTK timed out", invocation.row.name)
            return False
        except _OutputTooLarge:
            await _terminate_process(process)
            logger.warning("proxy_hook[%s]: RTK output exceeded limit", invocation.row.name)
            return False
        except asyncio.CancelledError:
            await _terminate_process(process)
            raise

        if code not in {0, 3}:
            detail_bytes = stderr or stdout
            detail = detail_bytes[:512].decode("utf-8", errors="replace").strip()
            logger.debug(
                "proxy_hook[%s]: RTK exited %s%s",
                invocation.row.name,
                code,
                f": {detail}" if detail else "",
            )
            return False
        if stderr:
            detail = stderr[:512].decode("utf-8", errors="replace").strip()
            logger.debug(
                "proxy_hook[%s]: RTK wrote to stderr%s",
                invocation.row.name,
                f": {detail}" if detail else "",
            )
            return False
        try:
            transformed = stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            logger.warning("proxy_hook[%s]: RTK output is not UTF-8", invocation.row.name)
            return False
        transformed = transformed.removesuffix("\n").removesuffix("\r")
        if transformed == command:
            return False
        if not _is_plausible_rewrite(transformed):
            detail = transformed[:512].strip()
            logger.debug(
                "proxy_hook[%s]: RTK output rejected%s",
                invocation.row.name,
                f": {detail}" if detail else "",
            )
            return False
        if _detaches_shell_context(command, transformed):
            logger.debug(
                "proxy_hook[%s]: RTK detached the shell context, keeping %s",
                invocation.row.name,
                _shell_context(command),
            )
            return False
        if _is_refused_worktree_git_rewrite(event, transformed):
            logger.debug(
                "proxy_hook[%s]: keeping bare git in linked worktree %s",
                invocation.row.name,
                event.cwd,
            )
            return False

        tool_input["command"] = transformed
        logger.debug(
            "proxy_hook[%s]: RTK transformed command for %s",
            invocation.row.name,
            event.source.value,
        )
        return True
