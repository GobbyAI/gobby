"""Typed domain models for provider capability snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TypedDict


class ReasoningSupport(StrEnum):
    """How confidently a model's reasoning support is known."""

    KNOWN = "known"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class SourceState(StrEnum):
    """Health of a source participating in a provider snapshot."""

    PENDING = "pending"
    OK = "ok"
    STALE = "stale"
    ERROR = "error"


class FactProvenanceData(TypedDict):
    source_key: str
    source_url: str | None
    observed_at: str


class ModelCapabilityData(TypedDict):
    canonical_model: str
    display_name: str
    aliases: list[str]
    available: bool
    hidden: bool
    is_default: bool
    context_length: int | None
    max_output_tokens: int | None
    reasoning: str
    supported_efforts: list[str] | None
    default_effort: str | None
    latency_class: str | None
    input_modalities: list[str] | None
    supports_tools: bool | None
    provenance: dict[str, FactProvenanceData]


class SourceHealthData(TypedDict):
    source_key: str
    source_url: str | None
    required: bool
    state: str
    attempts: int
    last_attempt_at: str | None
    last_success_at: str | None
    last_error: str | None


class ProviderSnapshotData(TypedDict):
    provider: str
    generation: int
    models: list[ModelCapabilityData]
    sources: list[SourceHealthData]


@dataclass(frozen=True)
class FactProvenance:
    """Origin and observation time for one capability fact."""

    source_key: str
    source_url: str | None
    observed_at: datetime

    def to_dict(self) -> FactProvenanceData:
        return {
            "source_key": self.source_key,
            "source_url": self.source_url,
            "observed_at": self.observed_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: FactProvenanceData) -> FactProvenance:
        return cls(
            source_key=data["source_key"],
            source_url=data["source_url"],
            observed_at=datetime.fromisoformat(data["observed_at"]),
        )


@dataclass(frozen=True)
class ModelCapability:
    """Canonical capability facts and execution routes for one model."""

    canonical_model: str
    display_name: str
    aliases: tuple[str, ...]
    available: bool
    hidden: bool
    is_default: bool
    context_length: int | None
    max_output_tokens: int | None
    reasoning: ReasoningSupport
    supported_efforts: tuple[str, ...] | None
    default_effort: str | None
    latency_class: str | None
    input_modalities: tuple[str, ...] | None
    supports_tools: bool | None
    provenance: Mapping[str, FactProvenance]

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(self.aliases))
        if self.supported_efforts is not None:
            object.__setattr__(self, "supported_efforts", tuple(self.supported_efforts))
        if self.input_modalities is not None:
            object.__setattr__(self, "input_modalities", tuple(self.input_modalities))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    def to_dict(self) -> ModelCapabilityData:
        return {
            "canonical_model": self.canonical_model,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "available": self.available,
            "hidden": self.hidden,
            "is_default": self.is_default,
            "context_length": self.context_length,
            "max_output_tokens": self.max_output_tokens,
            "reasoning": self.reasoning.value,
            "supported_efforts": (
                list(self.supported_efforts) if self.supported_efforts is not None else None
            ),
            "default_effort": self.default_effort,
            "latency_class": self.latency_class,
            "input_modalities": (
                list(self.input_modalities) if self.input_modalities is not None else None
            ),
            "supports_tools": self.supports_tools,
            "provenance": {
                name: provenance.to_dict() for name, provenance in self.provenance.items()
            },
        }

    @classmethod
    def from_dict(cls, data: ModelCapabilityData) -> ModelCapability:
        supported_efforts = data["supported_efforts"]
        input_modalities = data["input_modalities"]
        return cls(
            canonical_model=data["canonical_model"],
            display_name=data["display_name"],
            aliases=tuple(data["aliases"]),
            available=data["available"],
            hidden=data["hidden"],
            is_default=data["is_default"],
            context_length=data["context_length"],
            max_output_tokens=data["max_output_tokens"],
            reasoning=ReasoningSupport(data["reasoning"]),
            supported_efforts=(tuple(supported_efforts) if supported_efforts is not None else None),
            default_effort=data["default_effort"],
            latency_class=data["latency_class"],
            input_modalities=(tuple(input_modalities) if input_modalities is not None else None),
            supports_tools=data["supports_tools"],
            provenance={
                name: FactProvenance.from_dict(provenance)
                for name, provenance in data["provenance"].items()
            },
        )


@dataclass(frozen=True)
class SourceHealth:
    """Current refresh health for one provider capability source."""

    source_key: str
    source_url: str | None
    required: bool
    state: SourceState
    attempts: int
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None

    def to_dict(self) -> SourceHealthData:
        return {
            "source_key": self.source_key,
            "source_url": self.source_url,
            "required": self.required,
            "state": self.state.value,
            "attempts": self.attempts,
            "last_attempt_at": (
                self.last_attempt_at.isoformat() if self.last_attempt_at is not None else None
            ),
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at is not None else None
            ),
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: SourceHealthData) -> SourceHealth:
        last_attempt_at = data["last_attempt_at"]
        last_success_at = data["last_success_at"]
        return cls(
            source_key=data["source_key"],
            source_url=data["source_url"],
            required=data["required"],
            state=SourceState(data["state"]),
            attempts=data["attempts"],
            last_attempt_at=(
                datetime.fromisoformat(last_attempt_at) if last_attempt_at is not None else None
            ),
            last_success_at=(
                datetime.fromisoformat(last_success_at) if last_success_at is not None else None
            ),
            last_error=data["last_error"],
        )


@dataclass(frozen=True)
class ProviderSnapshot:
    """Atomic, immutable capability snapshot for one provider."""

    provider: str
    generation: int
    models: tuple[ModelCapability, ...]
    sources: tuple[SourceHealth, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", tuple(self.models))
        object.__setattr__(self, "sources", tuple(self.sources))

    def to_dict(self) -> ProviderSnapshotData:
        return {
            "provider": self.provider,
            "generation": self.generation,
            "models": [model.to_dict() for model in self.models],
            "sources": [source.to_dict() for source in self.sources],
        }

    @classmethod
    def from_dict(cls, data: ProviderSnapshotData) -> ProviderSnapshot:
        return cls(
            provider=data["provider"],
            generation=data["generation"],
            models=tuple(ModelCapability.from_dict(model) for model in data["models"]),
            sources=tuple(SourceHealth.from_dict(source) for source in data["sources"]),
        )
