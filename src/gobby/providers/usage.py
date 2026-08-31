"""Provider usage reporting and AGY's automation-safe usage source."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

_AGY_USAGE_COMMAND = ("agy", "-p", "/usage", "--output-format", "json")
_AGY_USAGE_TIMEOUT_SECONDS = 15.0

type RunCommand = Callable[[tuple[str, ...]], Awaitable[tuple[int, str, str]]]
type Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """One normalized provider capacity window."""

    label: str
    used: float
    limit: float
    unit: str
    resets_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "used": self.used,
            "limit": self.limit,
            "unit": self.unit,
            "resets_at": self.resets_at,
        }


@dataclass(frozen=True, slots=True)
class ProviderUsageSnapshot:
    """Successful raw observation from one provider usage reporter."""

    provider: str
    observed_at: datetime
    supported: bool
    windows: tuple[UsageWindow, ...]
    raw: dict[str, object]


class ProviderUsageReporter(Protocol):
    """Automation-safe source of one provider's current usage capacity."""

    @property
    def provider(self) -> str: ...

    async def report(self) -> ProviderUsageSnapshot: ...


class TransientUsageRefreshError(RuntimeError):
    """Typed transient failure to refresh an otherwise supported provider."""

    def __init__(self, provider: str, code: str, reason: str) -> None:
        self.provider = provider
        self.code = code
        self.reason = reason
        super().__init__(f"{provider} usage refresh failed ({code}): {reason}")


async def _run_agy_usage(command: tuple[str, ...]) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return (
        process.returncode if process.returncode is not None else 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


@dataclass(frozen=True, slots=True)
class AgyUsageReporter:
    """Read AGY capacity through the no-agent-turn ``/usage`` command."""

    run_command: RunCommand = _run_agy_usage
    clock: Clock = lambda: datetime.now(UTC)
    timeout_seconds: float = _AGY_USAGE_TIMEOUT_SECONDS

    @property
    def provider(self) -> str:
        return "agy"

    async def report(self) -> ProviderUsageSnapshot:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return_code, stdout, stderr = await self.run_command(_AGY_USAGE_COMMAND)
        except TimeoutError as error:
            raise TransientUsageRefreshError(
                self.provider,
                "timeout",
                f"timed out after {self.timeout_seconds:g} seconds",
            ) from error
        except OSError as error:
            raise TransientUsageRefreshError(
                self.provider,
                "command_failed",
                str(error),
            ) from error

        if return_code != 0:
            reason = stderr.strip() or stdout.strip() or f"exit status {return_code}"
            raise TransientUsageRefreshError(self.provider, "command_failed", reason)

        try:
            raw, windows = _parse_agy_usage(stdout)
        except (TypeError, ValueError) as error:
            raise TransientUsageRefreshError(
                self.provider,
                "invalid_payload",
                str(error),
            ) from error

        return ProviderUsageSnapshot(
            provider=self.provider,
            observed_at=self.clock(),
            supported=True,
            windows=windows,
            raw=raw,
        )


def _parse_agy_usage(stdout: str) -> tuple[dict[str, object], tuple[UsageWindow, ...]]:
    if not stdout.strip():
        raise ValueError("command returned empty stdout")
    try:
        payload: object = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"stdout is not valid JSON: {error.msg}") from error

    root = _mapping(payload, "command envelope")
    if root.get("status") != "SUCCESS":
        raise ValueError("command envelope status is not SUCCESS")
    if root.get("num_turns") != 0:
        raise ValueError("usage command unexpectedly started an agent turn")

    command = _mapping(root.get("command"), "command")
    if command.get("name") != "usage":
        raise ValueError("command envelope is not a usage result")
    data = _mapping(command.get("data"), "command data")
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("command data groups must be a non-empty list")

    windows: list[UsageWindow] = []
    for group_index, raw_group in enumerate(groups):
        group = _mapping(raw_group, f"usage group {group_index}")
        group_name = _required_string(group.get("name"), f"usage group {group_index} name")
        buckets = group.get("buckets")
        if not isinstance(buckets, list) or not buckets:
            raise ValueError(f"usage group {group_name!r} buckets must be a non-empty list")
        for bucket_index, raw_bucket in enumerate(buckets):
            bucket = _mapping(raw_bucket, f"usage group {group_name!r} bucket {bucket_index}")
            bucket_name = _required_string(
                bucket.get("name"),
                f"usage group {group_name!r} bucket {bucket_index} name",
            )
            remaining = _fraction(
                bucket.get("remaining_fraction"),
                f"usage bucket {bucket_name!r} remaining_fraction",
            )
            resets_at = bucket.get("reset_time")
            if resets_at is not None and not isinstance(resets_at, str):
                raise TypeError(f"usage bucket {bucket_name!r} reset_time must be a string or null")
            windows.append(
                UsageWindow(
                    label=f"{group_name} — {bucket_name}",
                    used=1.0 - remaining,
                    limit=1.0,
                    unit="fraction",
                    resets_at=resets_at,
                )
            )

    return dict(root), tuple(windows)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{name} must be an object")
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _fraction(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be numeric")
    fraction = float(value)
    if not 0.0 <= fraction <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return fraction
