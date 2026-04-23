"""Shared context for skill tool handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
