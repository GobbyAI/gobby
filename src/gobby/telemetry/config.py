"""
Telemetry configuration module.

Contains Pydantic models for:
- TelemetrySettings: Unified OTel and logging configuration.
- ExporterSettings: OTLP and Prometheus exporter settings.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LLMTracingConfig(BaseModel):
    """Configuration for LLM call auto-instrumentation."""

    enabled: bool = Field(
        default=False,
        description="Enable LLM call tracing via OpenLLMetry instrumentors",
    )
    capture_content: bool = Field(
        default=False,
        description="Capture prompt/completion content in spans (privacy-first default: off)",
    )
    providers: list[str] = Field(
        default_factory=lambda: ["anthropic", "openai"],
        description="LLM providers to instrument",
    )


class ExporterSettings(BaseModel):
    """Configuration for telemetry exporters."""

    otlp_endpoint: str | None = Field(
        default=None,
        description="OTLP collector endpoint (e.g., http://localhost:4317)",
    )
    otlp_protocol: Literal["grpc", "http"] = Field(
        default="grpc",
        description="OTLP transport protocol",
    )
    otlp_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Headers for OTLP exporter (e.g., Authorization for LangWatch)",
    )
    prometheus_enabled: bool = Field(
        default=True,
        description="Enable Prometheus metrics scraping endpoint",
    )


class TelemetrySettings(BaseModel):
    """Unified telemetry and logging configuration."""

    service_name: str = Field(
        default="gobby-daemon",
        description="Service name for OpenTelemetry resource",
    )

    # Logging settings (preserving legacy fields)
    log_level: Literal["debug", "info", "warning", "error"] = Field(
        default="info",
        description="Log level",
    )
    log_format: Literal["text", "json"] = Field(
        default="text",
        description="Log format (text or json)",
    )

    # Log file paths (preserved from legacy settings)
    log_file: str = Field(
        default="~/.gobby/logs/gobby.log",
        description="Daemon main log file path",
    )
    log_file_error: str = Field(
        default="~/.gobby/logs/gobby-error.log",
        description="Daemon error log file path",
    )
    log_file_hook_manager: str = Field(
        default="~/.gobby/logs/hook-manager.log",
        description="Claude Code hook manager log file path",
    )
    log_file_mcp_server: str = Field(
        default="~/.gobby/logs/mcp-server.log",
        description="MCP server log file path",
    )
    log_file_mcp_client: str = Field(
        default="~/.gobby/logs/mcp-client.log",
        description="MCP client connection log file path",
    )
    max_size_mb: int = Field(
        default=10,
        description="Maximum log file size in MB",
    )
    backup_count: int = Field(
        default=5,
        description="Number of backup log files to keep",
    )

    # Tracing settings
    traces_enabled: bool = Field(
        default=True,
        description="Enable distributed tracing",
    )
    traces_to_console: bool = Field(
        default=False,
        description="Export spans to console (for debugging)",
    )
    trace_sample_rate: float = Field(
        default=1.0,
        description="Trace sampling rate (0.0 to 1.0)",
    )
    trace_retention_days: int = Field(
        default=7,
        gt=0,
        description="Retention period for local trace spans (days)",
    )

    # Metrics settings
    metrics_enabled: bool = Field(
        default=True,
        description="Enable metrics collection",
    )

    # Exporter settings
    exporter: ExporterSettings = Field(
        default_factory=ExporterSettings,
        description="Telemetry exporter configuration",
    )

    # LLM tracing settings
    llm_tracing: LLMTracingConfig = Field(
        default_factory=LLMTracingConfig,
        description="LLM call auto-instrumentation via OpenLLMetry",
    )

    @field_validator("max_size_mb", "backup_count")
    @classmethod
    def validate_positive(cls, v: int) -> int:
        """Validate value is positive."""
        if v <= 0:
            raise ValueError("Value must be positive")
        return v

    @field_validator("trace_sample_rate")
    @classmethod
    def validate_sample_rate(cls, v: float) -> float:
        """Validate sample rate is between 0.0 and 1.0."""
        if not (0.0 <= v <= 1.0):
            raise ValueError("trace_sample_rate must be between 0.0 and 1.0")
        return v
