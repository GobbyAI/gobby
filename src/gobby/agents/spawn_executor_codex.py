"""Codex terminal spawn: launch, then deliver the assembled prompt."""

from __future__ import annotations

import asyncio
import logging

from gobby.agents.spawn_executor_providers import prepare_codex_spawn
from gobby.agents.spawn_executor_support import schedule_codex_prompt_delivery
from gobby.agents.spawn_models import SpawnRequest, SpawnResult

logger = logging.getLogger(__name__)


async def _spawn_codex_terminal(request: SpawnRequest) -> SpawnResult:
    # Deferred: spawn_executor imports this module for its provider dispatch.
    from gobby.agents.spawn_executor import _runtime_spawn

    plan = await prepare_codex_spawn(request)
    if isinstance(plan, SpawnResult):
        return plan
    if plan.inject_persona and request.session_manager is not None and not plan.codex_prompt:
        from gobby.workflows.state_manager import SessionVariableManager

        await asyncio.to_thread(
            SessionVariableManager(request.session_manager._storage.db).merge_variables,
            plan.child_session_id,
            {"_agent_context_injected": True},
        )
    result = await _runtime_spawn(request, plan)
    if result.success and plan.codex_prompt:
        if plan.inject_persona and request.session_manager is not None:
            from gobby.workflows.state_manager import SessionVariableManager

            await asyncio.to_thread(
                SessionVariableManager(request.session_manager._storage.db).merge_variables,
                plan.child_session_id,
                {"_agent_context_injected": True},
            )
        coordinator = request.write_coordinator
        manager = request.terminal_manager
        missing = [
            name
            for name, available in (
                ("terminal_id", result.terminal_id is not None),
                ("write_coordinator", coordinator is not None),
                ("terminal_manager", manager is not None),
                ("run_manager", request.run_manager is not None),
            )
            if not available
        ]
        terminal = None
        if not missing and result.terminal_id is not None and manager is not None:
            terminal = await asyncio.to_thread(manager.get, result.terminal_id)
            if terminal is None:
                missing.append("terminal_row")
        if missing or terminal is None or coordinator is None:
            result.success = False
            result.status = "failed"
            result.error = f"codex_prompt_delivery_unavailable: {', '.join(missing)}"
            logger.error(
                "Codex prompt delivery unavailable for run %s: %s", plan.agent_run_id, missing
            )
        elif schedule_codex_prompt_delivery(
            coordinator,
            terminal,
            plan.codex_prompt,
            plan.agent_run_id,
            request.run_manager,
            request.cleanup_agent,
        ):
            logger.info(
                "Codex prompt delivery scheduled for run %s terminal %s",
                plan.agent_run_id,
                terminal.id,
            )
        else:
            result.success = False
            result.status = "failed"
            result.error = "codex_prompt_delivery_unavailable: scheduling refused"
            logger.error("Codex prompt delivery scheduling refused for run %s", plan.agent_run_id)
    return result
