"""Provider capability domain and activation validation."""

from gobby.providers.capabilities.activation import (
    ActivationHandler,
    ActivationValidationError,
    register_activation_handler,
    validate_activation,
)
from gobby.providers.capabilities.models import (
    ActivationDescriptor,
    FactProvenance,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
    SpeedMode,
)

__all__ = [
    "ActivationDescriptor",
    "ActivationHandler",
    "ActivationValidationError",
    "FactProvenance",
    "ModelCapability",
    "ModelRoute",
    "ProviderSnapshot",
    "ReasoningSupport",
    "SourceHealth",
    "SourceState",
    "SpeedMode",
    "register_activation_handler",
    "validate_activation",
]
