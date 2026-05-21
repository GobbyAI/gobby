"""Skill storage and management.

This package provides the Skill dataclass and LocalSkillManager for storing
and retrieving skills from the PostgreSQL hub, following the Agent Skills specification
(agentskills.io) with SkillPort feature parity plus Gobby-specific extensions.
"""

from gobby.storage.skills._manager import LocalSkillManager
from gobby.storage.skills._models import ChangeEvent, Skill, SkillFile, SkillSourceType
from gobby.storage.skills._notifier import SkillChangeNotifier

__all__ = [
    "ChangeEvent",
    "Skill",
    "SkillFile",
    "SkillSourceType",
    "SkillChangeNotifier",
    "LocalSkillManager",
]
