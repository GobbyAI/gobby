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
    from gobby.storage.database import DatabaseProtocol

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

    db: DatabaseProtocol
    storage: LocalSkillManager
    notifier: SkillChangeNotifier
    session_manager: SessionManager
    search: SkillSearch
    updater: SkillUpdater
    loader: SkillLoader
    project_id: str | None
    hub_manager: HubManager | None
    run_db: RunDb | None = None

    async def run_sqlite(
        self,
        func: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        if self.run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self.run_db(func, *args, **kwargs)
