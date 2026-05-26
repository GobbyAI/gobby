"""Shared context for skill tool handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ParamSpec, Protocol, TypeVar

from gobby.skills.hubs.manager import HubManager
from gobby.skills.loader import SkillLoader
from gobby.skills.search import SkillSearch
from gobby.skills.updater import SkillUpdater
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager, SkillChangeNotifier

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

P = ParamSpec("P")
R = TypeVar("R")


class RunDb(Protocol):
    def __call__(
        self,
        func: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Awaitable[R]: ...


@dataclass
class SkillsContext:
    """Shared dependencies for skill tool handlers."""

    db: HubDatabase
    storage: LocalSkillManager
    notifier: SkillChangeNotifier
    session_manager: SessionManager
    search: SkillSearch
    updater: SkillUpdater
    loader: SkillLoader
    project_id: str | None
    hub_manager: HubManager | None
    db_runner: RunDb | None = None

    async def run_db(
        self,
        func: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        if self.db_runner is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self.db_runner(func, *args, **kwargs)

    async def get_active_skill_names(self, session_id: str) -> list[str] | None:
        """Return active skill names recorded for a session, when available."""
        from gobby.workflows.state_manager import SessionVariableManager

        def _get_active_names() -> object | None:
            resolved_id = self.session_manager.resolve_session_reference(
                session_id, project_id=self.project_id
            )
            sv_mgr = SessionVariableManager(self.db)
            variables = sv_mgr.get_variables(resolved_id)
            return variables.get("_active_skill_names") if variables else None

        names = await self.run_db(_get_active_names)
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            return None
        return names
