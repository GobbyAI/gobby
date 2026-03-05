"""Completion event registry and daemon-wake notifications.

This package provides push-based notifications for async operations
(pipelines, child agents) so agents never need to poll or block.
"""

from gobby.events.completion_registry import CompletionEventRegistry

__all__ = ["CompletionEventRegistry"]
