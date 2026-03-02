"""Skill storage and management.

This package provides the Skill dataclass and LocalSkillManager for storing
and retrieving skills from SQLite, following the Agent Skills specification
(agentskills.io) with SkillPort feature parity plus Gobby-specific extensions.
"""

from gobby.storage.skills._manager import LocalSkillManager
from gobby.storage.skills._models import ChangeEvent, Skill, SkillSourceType
from gobby.storage.skills._notifier import SkillChangeNotifier

__all__ = [
    "ChangeEvent",
    "Skill",
    "SkillSourceType",
    "SkillChangeNotifier",
    "LocalSkillManager",
]
