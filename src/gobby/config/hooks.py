"""Hook timeout budget configuration."""

from pydantic import BaseModel, Field


class HookTimeoutConfig(BaseModel):
    """Timeouts for daemon hook execution and provider hook clients."""

    adapter_timeout: float = Field(
        default=105.0,
        gt=0,
        description="Daemon endpoint timeout in seconds for synchronous hook execution.",
    )
    provider_timeout: int = Field(
        default=120,
        gt=0,
        description="Provider hook timeout in seconds emitted by supported installers.",
    )
