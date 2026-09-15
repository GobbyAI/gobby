"""External service integrations for Gobby.

This module provides integration classes that delegate to official MCP servers
for external services like GitHub.
"""

from gobby.integrations.github import GitHubIntegration

__all__ = ["GitHubIntegration"]
