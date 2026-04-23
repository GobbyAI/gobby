"""Configuration for the persistent conductor agent."""

from pydantic import Field

from gobby.config.feature_base import FeatureDefaultConfig, ModelTier


class ConductorConfig(FeatureDefaultConfig):
    """Persistent conductor agent configuration.

    The conductor is a tick-based ChatSession that receives cron ticks,
    checks task/pipeline states, and dispatches dev/QA agents.
    """

    enabled: bool = Field(default=False, description="Enable the persistent conductor agent")
    model: str = Field(default="haiku", description="LLM model for the conductor session")
    tier: ModelTier = Field(
        default=ModelTier.LOW,
        description="Complexity tier — determines fallback model when local provider fails",
    )
    tick_interval_seconds: int = Field(
        default=120, gt=0, description="Interval between conductor ticks (seconds)"
    )
    idle_timeout_seconds: int = Field(
        default=300, gt=0, description="Tear down conductor session after this many idle seconds"
    )
    skip_if_busy: bool = Field(default=True, description="Skip tick if conductor is mid-response")
