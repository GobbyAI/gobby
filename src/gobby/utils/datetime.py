"""Datetime helpers for storage and API boundaries."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

T = TypeVar("T", bound=type[Any])


def utc_now() -> datetime:
    """Return the current instant as a UTC-aware datetime."""
    return datetime.now(UTC)


def to_aware_utc(value: datetime) -> datetime:
    """Normalize a datetime to UTC, treating naive values as legacy UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_stored_datetime(value: object) -> datetime | None:
    """Parse a stored ISO timestamp and normalize it to UTC.

    Legacy rows may contain naive ISO strings. Treat those as UTC so arithmetic
    against aware ``datetime.now(UTC)`` stays valid. Malformed string values
    raise ``ValueError`` from ``datetime.fromisoformat``. Non-string, non-datetime
    values (including mock objects) return ``None``.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_aware_utc(value)
    if not isinstance(value, str):
        return None
    return to_aware_utc(datetime.fromisoformat(value))


def require_stored_datetime(value: object, field_name: str = "timestamp") -> datetime:
    """Parse a required stored timestamp and normalize it to UTC."""
    parsed = parse_stored_datetime(value)
    if parsed is None:
        raise ValueError(f"{field_name} is required")
    return parsed


def datetime_to_iso(value: datetime | None) -> str | None:
    """Serialize a datetime for an external JSON/text boundary."""
    if value is None:
        return None
    return to_aware_utc(value).isoformat()


def datetime_to_required_iso(value: datetime) -> str:
    """Serialize a required datetime for an external JSON/text boundary."""
    return to_aware_utc(value).isoformat()


def datetime_to_local_iso(value: datetime | None) -> str | None:
    """Serialize a stored UTC datetime for a human reader, in host local time.

    Storage stays UTC; only presentation shifts. The rendered offset keeps the
    instant unambiguous.
    """
    if value is None:
        return None
    return to_aware_utc(value).astimezone().isoformat()


def is_valid_timezone(value: str) -> bool:
    """Return whether a string names a resolvable IANA timezone."""
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return False
    return True


def resolve_local_timezone(configured_timezone: str | None = None) -> str:
    """Resolve the host's IANA timezone name for schedule evaluation.

    An explicit configured value wins, then ``TZ``, then the ``/etc/localtime``
    symlink target, falling back to UTC. Timestamps remain stored as UTC — this
    only names the zone a wall-clock schedule is interpreted in.
    """
    configured = (configured_timezone or "").strip()
    if configured and is_valid_timezone(configured):
        return configured

    env_timezone = (os.environ.get("TZ") or "").strip()
    if env_timezone and not env_timezone.startswith(":") and is_valid_timezone(env_timezone):
        return env_timezone

    try:
        target = Path("/etc/localtime").resolve(strict=True)
    except OSError:
        return "UTC"

    parts = target.parts
    if "zoneinfo" not in parts:
        return "UTC"
    zoneinfo_index = parts.index("zoneinfo")
    candidate = "/".join(parts[zoneinfo_index + 1 :])
    if candidate and is_valid_timezone(candidate):
        return candidate
    return "UTC"


def to_json_safe(value: Any) -> Any:
    """Recursively serialize datetime/date values for JSON boundaries."""
    if isinstance(value, datetime):
        return datetime_to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [to_json_safe(item) for item in value]
    return value


def to_json_safe_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize a mapping for JSON boundaries while preserving dict typing."""
    return {str(key): to_json_safe(item) for key, item in value.items()}


def normalize_datetime_attrs(
    instance: object,
    *,
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
) -> None:
    """Normalize datetime attributes on mutable or frozen dataclass instances."""
    for field_name in required:
        object.__setattr__(
            instance,
            field_name,
            require_stored_datetime(getattr(instance, field_name), field_name),
        )
    for field_name in optional:
        object.__setattr__(
            instance,
            field_name,
            parse_stored_datetime(getattr(instance, field_name)),
        )


def normalize_datetime_model(
    *,
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
    serializers: Iterable[str] = ("to_dict", "to_brief", "to_prompt_dict"),
) -> Callable[[T], T]:
    """Decorate a row model so timestamp attrs are datetime-native."""
    required_fields = tuple(required)
    optional_fields = tuple(optional)
    serializer_names = tuple(serializers)

    def decorate(cls: T) -> T:
        original_init = cast(Callable[..., None], cls.__init__)

        @wraps(original_init)
        def __init__(self: object, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            normalize_datetime_attrs(
                self,
                required=required_fields,
                optional=optional_fields,
            )

        cls.__init__ = __init__

        for serializer_name in serializer_names:
            maybe_serializer = getattr(cls, serializer_name, None)
            if not callable(maybe_serializer):
                continue
            original_serializer = cast(Callable[..., Any], maybe_serializer)

            def serializer(
                self: object,
                *args: Any,
                __original_serializer: Callable[..., Any] = original_serializer,
                **kwargs: Any,
            ) -> Any:
                result = __original_serializer(self, *args, **kwargs)
                if isinstance(result, Mapping):
                    return to_json_safe_dict(result)
                return to_json_safe(result)

            setattr(cls, serializer_name, serializer)

        return cls

    return decorate
