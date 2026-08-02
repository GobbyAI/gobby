"""Typed PostgreSQL client pool bootstrap configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PostgresPoolConfig:
    """Settings used to construct and open the PostgreSQL client pool."""

    min_size: int = 2
    max_size: int = 20
    acquire_timeout_seconds: float = 5.0
    open_timeout_seconds: float = 30.0
    max_lifetime_seconds: float = 300.0

    def __post_init__(self) -> None:
        _validate_positive_int(self.min_size, "postgres_pool.min_size")
        _validate_positive_int(self.max_size, "postgres_pool.max_size")
        _validate_positive_float(
            self.acquire_timeout_seconds,
            "postgres_pool.acquire_timeout_seconds",
        )
        _validate_positive_float(
            self.open_timeout_seconds,
            "postgres_pool.open_timeout_seconds",
        )
        _validate_positive_float(
            self.max_lifetime_seconds,
            "postgres_pool.max_lifetime_seconds",
        )
        if self.min_size > self.max_size:
            raise ValueError("postgres_pool.min_size must be less than or equal to max_size")

    def to_dict(self) -> dict[str, int | float]:
        """Return the YAML/Pydantic representation of these settings."""
        return {
            "min_size": self.min_size,
            "max_size": self.max_size,
            "acquire_timeout_seconds": self.acquire_timeout_seconds,
            "open_timeout_seconds": self.open_timeout_seconds,
            "max_lifetime_seconds": self.max_lifetime_seconds,
        }


def postgres_pool_config_from_mapping(data: object) -> PostgresPoolConfig:
    """Parse a bootstrap mapping into validated pool settings."""
    if data is None:
        return DEFAULT_POSTGRES_POOL_CONFIG
    if not isinstance(data, dict):
        raise ValueError("postgres_pool must be a mapping")

    defaults = DEFAULT_POSTGRES_POOL_CONFIG
    return PostgresPoolConfig(
        min_size=_parse_int(data.get("min_size", defaults.min_size), "postgres_pool.min_size"),
        max_size=_parse_int(data.get("max_size", defaults.max_size), "postgres_pool.max_size"),
        acquire_timeout_seconds=_parse_float(
            data.get("acquire_timeout_seconds", defaults.acquire_timeout_seconds),
            "postgres_pool.acquire_timeout_seconds",
        ),
        open_timeout_seconds=_parse_float(
            data.get("open_timeout_seconds", defaults.open_timeout_seconds),
            "postgres_pool.open_timeout_seconds",
        ),
        max_lifetime_seconds=_parse_float(
            data.get("max_lifetime_seconds", defaults.max_lifetime_seconds),
            "postgres_pool.max_lifetime_seconds",
        ),
    )


def _parse_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _parse_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a number")
    return float(value)


def _validate_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _validate_positive_float(value: float, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be a positive finite number")


DEFAULT_POSTGRES_POOL_CONFIG = PostgresPoolConfig()
