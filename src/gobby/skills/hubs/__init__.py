"""Skill hub providers for searching and installing skills from registries."""

from gobby.skills.hubs.base import (
    DownloadResult,
    HubProvider,
    HubSkillDetails,
    HubSkillInfo,
)
from gobby.skills.hubs.claude_plugins import ClaudePluginsProvider
from gobby.skills.hubs.clawdhub import ClawdHubProvider
from gobby.skills.hubs.github_collection import GitHubCollectionProvider
from gobby.skills.hubs.manager import HubManager
from gobby.skills.hubs.skillsmp import SkillsMPProvider

__all__ = [
    "ClaudePluginsProvider",
    "ClawdHubProvider",
    "DownloadResult",
    "GitHubCollectionProvider",
    "HubManager",
    "HubProvider",
    "HubSkillDetails",
    "HubSkillInfo",
    "SkillsMPProvider",
]
