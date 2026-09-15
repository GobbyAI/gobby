"""Sync services for external integrations.

This module provides sync services that orchestrate between gobby tasks
and external services like GitHub.
"""

from gobby.sync.github import GitHubSyncError, GitHubSyncService

__all__ = [
    "GitHubSyncService",
    "GitHubSyncError",
]
