"""Trusted, permission-neutral command transformation handlers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from gobby.adapters.capabilities import get_provider_capabilities
from gobby.hooks.effect_deadline import remaining_blocking_effect_seconds
from gobby.hooks.events import HookEvent
from gobby.integrations.rtk import resolve_rtk
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.workflows.definitions import RuleEffect
from gobby.workflows.engine._offload import offload

logger = logging.getLogger(__name__)

_DEFAULT_PROXY_TIMEOUT_SECONDS = 2.0
_MAX_PROXY_OUTPUT_BYTES = 64 * 1024


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


class ProxyHooksMixin:
    """Execute an internal registry of trusted command transformers."""

    async def _run_proxy_hooks(
        self,
        invocations: list[ProxyHookInvocation],
        event: HookEvent,
        *,
        blocking_deadline: float | None,
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
        blocking_deadline: float | None,
    ) -> bool:
        tool_input = event.data.get("tool_input")
        if not isinstance(tool_input, dict):
            return False
        command = tool_input.get("command")
        if not isinstance(command, str):
            return False

        maximum = invocation.effect.timeout_seconds or _DEFAULT_PROXY_TIMEOUT_SECONDS
        timeout = remaining_blocking_effect_seconds(blocking_deadline, maximum=maximum)
        if timeout <= 0:
            logger.warning("proxy_hook[%s]: blocking deadline exhausted", invocation.row.name)
            return False

        probe_timeout = min(0.5, max(timeout / 4, 0.05))
        probe = await offload(resolve_rtk, timeout=probe_timeout)
        if probe is None:
            logger.warning(
                "proxy_hook[%s]: compatible RTK executable unavailable", invocation.row.name
            )
            return False

        timeout = remaining_blocking_effect_seconds(blocking_deadline, maximum=maximum)
        if timeout <= 0:
            logger.warning("proxy_hook[%s]: blocking deadline exhausted", invocation.row.name)
            return False

        argv = [
            str(probe.path),
            "hook",
            "check",
            "--agent",
            event.source.value,
            command,
        ]
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

        if code == 1:
            return False
        if code != 0:
            detail = stderr[:512].decode("utf-8", errors="replace").strip()
            logger.warning(
                "proxy_hook[%s]: RTK exited %s%s",
                invocation.row.name,
                code,
                f": {detail}" if detail else "",
            )
            return False
        try:
            transformed = stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            logger.warning("proxy_hook[%s]: RTK output is not UTF-8", invocation.row.name)
            return False
        transformed = transformed.removesuffix("\n").removesuffix("\r")
        if not transformed or transformed == command:
            return False

        tool_input["command"] = transformed
        logger.info(
            "proxy_hook[%s]: RTK transformed command for %s",
            invocation.row.name,
            event.source.value,
        )
        return True
