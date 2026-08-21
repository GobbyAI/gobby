"""Image-bearing request admission for ``text_generate`` bindings.

Transport eligibility is static binding metadata. Modality eligibility for
local endpoint bindings (lmstudio / ollama / vllm) is re-validated per request
through a :data:`LocalModalityResolver`: the candidate model is resolved to the
served id (vllm ``model: auto`` → the single served model), activation probe
evidence for that model wins, live discovery's advertised modalities fill the
rest, and neither means the candidate is not eligible. Bindings without a
resolver (or outside the discovery protocols) fall back to the static
predicate over binding metadata.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from gobby.ai.registry import CapabilityBinding

__all__ = [
    "FEATURE_CLI_PROVIDERS",
    "IMAGE_ELIGIBLE_PROTOCOLS",
    "LOCAL_DISCOVERY_PROTOCOLS",
    "VLLM_AUTO_MODEL",
    "LocalModalityResolver",
    "LocalModelModalities",
    "binding_endpoint_name",
    "binding_input_modalities",
    "image_admission_diagnostic",
    "image_candidate_eligible",
    "image_transport_eligible",
]

# Spawned-CLI feature lanes never carry image payloads.
FEATURE_CLI_PROVIDERS: frozenset[str] = frozenset({"agy", "droid", "grok", "qwen"})
IMAGE_ELIGIBLE_PROTOCOLS: frozenset[str] = frozenset(
    {"openai-compatible", "vllm", "lmstudio", "ollama"}
)
# Protocols whose served catalog (and advertised modalities) is discoverable live.
LOCAL_DISCOVERY_PROTOCOLS: frozenset[str] = frozenset({"lmstudio", "ollama", "vllm"})
VLLM_AUTO_MODEL = "auto"


@dataclass(frozen=True)
class LocalModelModalities:
    """Served model id and its authoritative input modalities for one candidate."""

    model: str | None
    input_modalities: tuple[str, ...] | None
    error: str | None = None


# (endpoint_name, requested model or None for the endpoint default) -> evidence.
LocalModalityResolver = Callable[[str, str | None], Awaitable[LocalModelModalities]]


def binding_endpoint_name(binding: CapabilityBinding) -> str | None:
    endpoint = binding.metadata.get("endpoint")
    if isinstance(endpoint, str) and endpoint:
        return endpoint
    if binding.provider.startswith("endpoint:"):
        return binding.provider.removeprefix("endpoint:") or None
    return None


def binding_input_modalities(binding: CapabilityBinding) -> tuple[str, ...] | None:
    raw = binding.metadata.get("input_modalities")
    if raw is None:
        return None
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Sequence) and not isinstance(raw, bytes | bytearray):
        return tuple(str(item) for item in raw)
    return None


def image_transport_eligible(binding: CapabilityBinding) -> bool:
    """Return whether a binding's transport may carry image inputs."""
    if binding.provider in FEATURE_CLI_PROVIDERS:
        return False
    if binding.provider == "claude":
        return True
    if binding_endpoint_name(binding) is None:
        return False
    if binding.metadata.get("wire_api") == "responses":
        return True
    return binding.metadata.get("protocol") in IMAGE_ELIGIBLE_PROTOCOLS


def _binding_default_model(binding: CapabilityBinding) -> str | None:
    model = binding.metadata.get("model")
    return model if isinstance(model, str) and model else None


def _modalities_clause(modalities: Sequence[str] | None) -> str:
    rendered = None if modalities is None else list(modalities)
    return f"input_modalities {rendered} do not include 'image'"


def _static_modality_reason(binding: CapabilityBinding, model: str | None) -> str | None:
    """Return why binding metadata rejects ``model`` for images, or None when eligible."""
    modalities = binding_input_modalities(binding)
    if modalities is None or "image" not in modalities:
        return _modalities_clause(modalities)
    probed_model = binding.metadata.get("probed_model")
    if not isinstance(probed_model, str) or not probed_model:
        return None
    requested = model if model is not None else _binding_default_model(binding)
    if requested is None or requested == probed_model:
        return None
    if binding.metadata.get("protocol") == "vllm" and requested == VLLM_AUTO_MODEL:
        return None
    return f"probe evidence covers model {probed_model!r}, not {requested!r}"


def image_candidate_eligible(
    binding: CapabilityBinding,
    *,
    model: str | None = None,
) -> bool:
    """Return whether binding metadata alone admits ``model`` for an image request.

    ``model=None`` means the binding's default model. This is the static
    verdict; :func:`image_admission_diagnostic` re-validates local endpoint
    bindings live when a resolver is available.
    """
    if not image_transport_eligible(binding):
        return False
    if binding.provider == "claude":
        return True
    return _static_modality_reason(binding, model) is None


def _diagnostic(binding: CapabilityBinding, model: str | None, reason: str) -> str:
    label = f"{binding.provider}/{model}" if model else binding.provider
    return f"Image inputs are not supported by {label}: {reason}"


async def image_admission_diagnostic(
    binding: CapabilityBinding,
    model: str | None,
    resolver: LocalModalityResolver | None,
) -> str | None:
    """Return None when ``binding`` may serve an image request for ``model``.

    Otherwise return the user-facing diagnostic explaining the rejection.
    """
    if not image_transport_eligible(binding):
        return _diagnostic(binding, model, "binding is not an image-eligible transport")
    if binding.provider == "claude":
        return None
    endpoint_name = binding_endpoint_name(binding)
    if (
        resolver is not None
        and endpoint_name is not None
        and binding.metadata.get("protocol") in LOCAL_DISCOVERY_PROTOCOLS
    ):
        resolved = await resolver(endpoint_name, model)
        if resolved.error is not None:
            return _diagnostic(binding, model, resolved.error)
        if resolved.input_modalities is None or "image" not in resolved.input_modalities:
            return _diagnostic(
                binding,
                resolved.model or model,
                _modalities_clause(resolved.input_modalities),
            )
        return None
    reason = _static_modality_reason(binding, model)
    return None if reason is None else _diagnostic(binding, model, reason)
