"""Provider capability collector contracts and validation."""

from gobby.providers.capabilities.collectors.base import (
    CapabilityCollector,
    SnapshotValidationError,
    SourceSpec,
    collectors,
    register_collector,
    validate_snapshot,
)

__all__ = [
    "CapabilityCollector",
    "SnapshotValidationError",
    "SourceSpec",
    "collectors",
    "register_collector",
    "validate_snapshot",
]
