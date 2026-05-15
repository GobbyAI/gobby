"""Shared context for skill tool handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.skills.hubs.manager import HubManager
from gobby.skills.loader import SkillLoader
from gobby.skills.search import SkillSearch
from gobby.skills.updater import SkillUpdater
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager, SkillChangeNotifier

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol


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
    run_db: Callable[..., Awaitable[Any]] | None = None

    async def run_sqlite(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self.run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self.run_db(func, *args, **kwargs)
