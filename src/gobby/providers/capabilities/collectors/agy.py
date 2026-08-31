"""AGY model capability discovery through the CLI's JSON command envelope."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from gobby.providers.capabilities.collectors.base import SourceSpec
from gobby.providers.capabilities.models import (
    FactProvenance,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
    SpeedMode,
)
from gobby.providers.version_gate import AgySupportRecord, peek_agy_support
from gobby.servers.provider_model_defaults import AGY_MODELS

_SOURCE_KEY = "agy_models_cli"
_BUNDLED_SOURCE_KEY = "bundled"
_COMMAND = ("agy", "--output-format", "json", "models")
_EFFORT_SUFFIXES = ("high", "medium", "low")
_MODEL_FACTS = (
    "canonical_model",
    "display_name",
    "aliases",
    "available",
    "hidden",
    "is_default",
    "context_length",
    "reasoning",
    "supported_efforts",
    "default_effort",
)
_ROUTE_FACTS = (
    "speed_mode",
    "selector",
    "available",
    "latency_class",
    "activations",
)
_CONTEXT_WINDOWS = {
    "gemini-3.7-flash": 1_048_576,
    "gemini-3.6-flash": 1_048_576,
    "gemini-3.5-flash": 1_048_576,
    "gemini-3.1-pro": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6-thinking": 1_000_000,
    "gpt-oss-120b": 131_072,
}

type RunCommand = Callable[[tuple[str, ...]], Awaitable[tuple[int, str, str]]]
type SupportRecord = Callable[[], AgySupportRecord]
type Clock = Callable[[], datetime]


class AgySourceError(ValueError):
    """Typed failure from the AGY model discovery source."""

    def __init__(self, code: str, detail: str) -> None:
        self.source_key = _SOURCE_KEY
        self.code = code
        super().__init__(f"AGY source {_SOURCE_KEY!r} failed ({code}): {detail}")


@dataclass(frozen=True, slots=True)
class _AgyModel:
    model_id: str
    label: str


async def _run_agy_models(command: tuple[str, ...]) -> tuple[int, str, str]:
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


@dataclass(frozen=True)
class AgyCollector:
    """Build an AGY capability snapshot from ``agy models``."""

    run_command: RunCommand = _run_agy_models
    support_record: SupportRecord = peek_agy_support
    clock: Clock = lambda: datetime.now(UTC)

    provider = "agy"
    sources = (SourceSpec(_SOURCE_KEY, None, required=True),)

    async def collect(self) -> ProviderSnapshot:
        support = self.support_record()
        if not support.supported:
            code = (
                "binary_unavailable" if support.installed_version is None else "unsupported_version"
            )
            raise AgySourceError(code, support.reason)

        try:
            return_code, stdout, stderr = await self.run_command(_COMMAND)
        except FileNotFoundError as error:
            raise AgySourceError("binary_unavailable", str(error)) from error
        except OSError as error:
            raise AgySourceError("command_failed", str(error)) from error

        if return_code != 0:
            detail = stderr.strip() or stdout.strip() or f"exit status {return_code}"
            code = (
                "unauthenticated"
                if not stdout.strip() and "please sign in" in stderr.casefold()
                else "command_failed"
            )
            raise AgySourceError(code, detail)

        observed_at = self.clock()
        try:
            raw_models = _parse_models(stdout)
            models = tuple(_build_model(model, observed_at) for model in raw_models)
        except (TypeError, ValueError) as error:
            raise AgySourceError("invalid_payload", str(error)) from error

        return ProviderSnapshot(
            provider=self.provider,
            generation=0,
            models=models,
            sources=(_healthy_source(observed_at),),
        )


def _parse_models(stdout: str) -> tuple[_AgyModel, ...]:
    if not stdout.strip():
        raise ValueError("command returned empty stdout")
    try:
        payload: object = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"stdout is not valid JSON: {error.msg}") from error
    root = _mapping(payload, "command envelope")
    if root.get("status") != "SUCCESS":
        raise ValueError("command envelope status is not SUCCESS")
    command = _mapping(root.get("command"), "command")
    if command.get("name") != "models":
        raise ValueError("command envelope is not a models result")
    data = _mapping(command.get("data"), "command data")
    entries = data.get("models")
    if not isinstance(entries, list) or not entries:
        raise ValueError("command data models must be a non-empty list")

    result: list[_AgyModel] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        item = _mapping(entry, f"model entry {index}")
        model_id = _required_string(item.get("id"), f"model entry {index} id")
        label = _required_string(item.get("label"), f"model {model_id!r} label")
        if model_id in seen:
            raise ValueError(f"duplicate model id {model_id!r}")
        seen.add(model_id)
        result.append(_AgyModel(model_id, label))
    return tuple(result)


def _build_model(raw: _AgyModel, observed_at: datetime) -> ModelCapability:
    base_model, effort = _split_effort(raw.model_id)
    context_length = _CONTEXT_WINDOWS.get(base_model)
    model_facts = set(_MODEL_FACTS)
    if context_length is None:
        model_facts.remove("context_length")
    if effort is None:
        model_facts.difference_update({"supported_efforts", "default_effort"})

    provenance = _live_provenance(model_facts, observed_at)
    if context_length is not None:
        provenance["context_length"] = _bundled_provenance(observed_at)
    aliases: tuple[str, ...] = ()
    if effort is not None:
        # The bare base name resolves to exactly one variant: the one carrying
        # the bundled table's default effort for that base model.
        aliases = (base_model,) if effort == _bundled_default_effort(base_model) else ()
        provenance["aliases"] = _bundled_provenance(observed_at)
    return ModelCapability(
        canonical_model=raw.model_id,
        display_name=raw.label,
        aliases=aliases,
        available=True,
        hidden=False,
        is_default=False,
        context_length=context_length,
        max_output_tokens=None,
        reasoning=(ReasoningSupport.KNOWN if effort is not None else ReasoningSupport.UNSUPPORTED),
        supported_efforts=(effort,) if effort is not None else None,
        default_effort=effort,
        latency_class=None,
        input_modalities=None,
        supports_tools=None,
        routes=(_standard_route(raw.model_id, observed_at),),
        provenance=provenance,
    )


def _bundled_default_effort(base_model: str) -> str | None:
    """Return the bundled default effort for a base model, or None when unbundled."""
    entry = AGY_MODELS.get(base_model)
    if entry is None:
        return None
    reasoning = entry.get("reasoning")
    default = reasoning.get("default_effort") if isinstance(reasoning, dict) else None
    return default.strip().lower() if isinstance(default, str) and default.strip() else None


def _bundled_provenance(observed_at: datetime) -> FactProvenance:
    return FactProvenance(source_key=_BUNDLED_SOURCE_KEY, source_url=None, observed_at=observed_at)


def _split_effort(model_id: str) -> tuple[str, str | None]:
    for effort in _EFFORT_SUFFIXES:
        suffix = f"-{effort}"
        if model_id.endswith(suffix):
            return model_id.removesuffix(suffix), effort
    return model_id, None


def _standard_route(selector: str, observed_at: datetime) -> ModelRoute:
    return ModelRoute(
        speed_mode=SpeedMode.STANDARD,
        selector=selector,
        available=True,
        usage_multiplier=None,
        throughput_multiplier=None,
        latency_class=None,
        activations=(),
        provenance=_live_provenance(_ROUTE_FACTS, observed_at),
    )


def _live_provenance(
    facts: Sequence[str] | set[str],
    observed_at: datetime,
) -> dict[str, FactProvenance]:
    return {
        fact: FactProvenance(source_key=_SOURCE_KEY, source_url=None, observed_at=observed_at)
        for fact in facts
    }


def _healthy_source(observed_at: datetime) -> SourceHealth:
    return SourceHealth(
        source_key=_SOURCE_KEY,
        source_url=None,
        required=True,
        state=SourceState.OK,
        attempts=1,
        last_attempt_at=observed_at,
        last_success_at=observed_at,
        last_error=None,
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()
