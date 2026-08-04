"""Database concurrency configuration stored in ConfigStore."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ConcurrencyValue = Literal["auto"] | int


class DatabaseConcurrencyConfig(BaseModel):
    """Restart-required limits for one daemon's PostgreSQL consumers."""

    pool_max_size: ConcurrencyValue = Field(
        default="auto",
        description="Runtime PostgreSQL pool maximum, or 'auto' for capacity sizing.",
    )
    executor_max_workers: ConcurrencyValue = Field(
        default="auto",
        description="Blocking database worker count, or 'auto' for CPU sizing.",
    )
    coverage_max_concurrency: ConcurrencyValue = Field(
        default="auto",
        description="Concurrent plan coverage evaluations, or 'auto' for CPU sizing.",
    )

    @field_validator(
        "pool_max_size",
        "executor_max_workers",
        "coverage_max_concurrency",
        mode="before",
    )
    @classmethod
    def validate_concurrency_value(cls, value: object) -> object:
        if value == "auto":
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be 'auto' or an integer")
        if value < 1:
            raise ValueError("must be 'auto' or a positive integer")
        return value


__all__ = ["ConcurrencyValue", "DatabaseConcurrencyConfig"]
