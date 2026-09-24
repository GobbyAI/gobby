"""Codex terminal spawn: launch, then deliver the assembled prompt."""

from __future__ import annotations

import asyncio

from gobby.agents.spawn_executor_providers import prepare_codex_spawn
from gobby.agents.spawn_executor_support import schedule_codex_prompt_delivery
from gobby.agents.spawn_models import SpawnRequest, SpawnResult


async def _spawn_codex_terminal(request: SpawnRequest) -> SpawnResult:
    # Deferred: spawn_executor imports this module for its provider dispatch.
    from gobby.agents.spawn_executor import _runtime_spawn

    plan = await prepare_codex_spawn(request)
    if isinstance(plan, SpawnResult):
        return plan
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
        if result.terminal_id and coordinator is not None and manager is not None:
            terminal = await asyncio.to_thread(manager.get, result.terminal_id)
            if terminal is not None:
                schedule_codex_prompt_delivery(
                    coordinator,
                    terminal,
                    plan.codex_prompt,
                    plan.agent_run_id,
                    request.run_manager,
                )
    return result
