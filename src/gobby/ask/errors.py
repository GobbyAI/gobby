"""Errors shared by the managed native Ask runtime boundary."""


class AskPermissionDenied(PermissionError):
    """An authenticated Ask agent exceeded its immutable stage authority."""


class AskRunNotFound(ValueError):
    """The requested Ask run does not exist in the authenticated project."""


class AskLifecycleConflict(ValueError):
    """The requested operation conflicts with the Ask run's durable lifecycle."""


class UnsupportedAskRuntime(RuntimeError):
    """A native provider cannot enforce Ask's MCP-only action surface."""


class EvidenceAdmissionError(RuntimeError):
    """Evidence invocation failed admission, execution, or contract validation."""
