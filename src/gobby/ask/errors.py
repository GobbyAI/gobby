"""Errors shared by the managed native Ask runtime boundary."""


class AskPermissionDenied(PermissionError):
    """An authenticated Ask agent exceeded its immutable stage authority."""


class UnsupportedAskRuntime(RuntimeError):
    """A native provider cannot enforce Ask's MCP-only action surface."""
