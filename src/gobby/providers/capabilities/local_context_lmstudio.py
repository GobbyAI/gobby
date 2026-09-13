"""Read-only context discovery using LM Studio's native model catalog."""

from __future__ import annotations

from typing import Any

import httpx

from gobby.providers.capabilities.local_context import (
    ContextDiagnostic as Diagnostic,
)
from gobby.providers.capabilities.local_context import (
    LocalContextIdentity,
    LocalContextInstance,
    LocalContextObservation,
    build_context_observation,
    positive_limit,
)

_MODELS_PATH = "/api/v1/models"
_TIMEOUT = 10.0


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _instance(
    model: dict[str, Any], raw: object, path: str | None, model_path: str
) -> LocalContextInstance:
    diagnostics: set[Diagnostic] = set()
    canonical = positive_limit(model.get("max_context_length"))
    if canonical is None:
        diagnostics.add(Diagnostic.CANONICAL_UNKNOWN)
        if model.get("max_context_length") is not None:
            diagnostics.add(Diagnostic.INVALID_METADATA)
    record = raw if isinstance(raw, dict) else {}
    instance_id = _text(record.get("id"))
    config = record.get("config")
    runtime = positive_limit(config.get("context_length")) if isinstance(config, dict) else None
    if path is not None and (instance_id is None or not isinstance(config, dict)):
        diagnostics.add(Diagnostic.INVALID_METADATA)
    if runtime is None:
        diagnostics.add(Diagnostic.RUNTIME_UNKNOWN)
        if isinstance(config, dict) and config.get("context_length") is not None:
            diagnostics.add(Diagnostic.INVALID_METADATA)
    provenance = {
        "canonical_limit": f"{model_path}.max_context_length",
        "model_id": f"{model_path}.key",
    }
    if path is not None:
        provenance["runtime_limit"] = f"{path}.config.context_length"
        provenance["instance_id"] = f"{path}.id"
    # Revision is not interchangeable with digest; preserve both when exposed.
    for field in ("digest", "revision", "selected_variant"):
        if any(
            source.get(field) is not None and _text(source[field]) is None
            for source in (model, record)
        ):
            diagnostics.add(Diagnostic.INVALID_METADATA)
        model_value = _text(model.get(field))
        value = _text(record.get(field)) or model_value
        if value is not None:
            provenance[field] = value
            provenance[f"{field}_source"] = (
                f"{path}.{field}" if _text(record.get(field)) else f"{model_path}.{field}"
            )
        if model_value and _text(record.get(field)) and model_value != record[field]:
            diagnostics.add(Diagnostic.IDENTITY_CONFLICT)
    quantization = model.get("quantization")
    if quantization is not None:
        if not isinstance(quantization, dict):
            diagnostics.add(Diagnostic.INVALID_METADATA)
        elif (name := _text(quantization.get("name"))) is not None:
            provenance["quantization"] = name
        elif quantization.get("name") is not None:
            diagnostics.add(Diagnostic.INVALID_METADATA)
    return LocalContextInstance(
        model_id=model["key"],
        instance_id=instance_id,
        digest=provenance.get("digest"),
        canonical_limit=canonical,
        runtime_limit=runtime,
        provenance=provenance,
        diagnostics=tuple(diagnostics),
    )


def _catalog_evidence(
    payload: object, model_id: str, instance_id: str | None
) -> tuple[list[LocalContextInstance], set[Diagnostic]]:
    diagnostics: set[Diagnostic] = set()
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return [], {Diagnostic.INVALID_METADATA}
    instances: list[LocalContextInstance] = []
    matched = False
    for index, model in enumerate(payload["models"]):
        if not isinstance(model, dict) or model.get("key") != model_id:
            continue
        if model.get("type") != "llm":
            if model.get("type") != "embedding":
                diagnostics.add(Diagnostic.INVALID_METADATA)
            continue
        matched = True
        model_path = f"{_MODELS_PATH}.models[{index}]"
        loaded = model.get("loaded_instances")
        if not isinstance(loaded, list):
            diagnostics.add(Diagnostic.INVALID_METADATA)
            loaded = []
        if not loaded:
            # Preserve unloaded duplicate records as unknown runtime evidence.
            instances.append(_instance(model, None, None, model_path))
        for position, raw in enumerate(loaded):
            evidence = _instance(
                model, raw, f"{model_path}.loaded_instances[{position}]", model_path
            )
            instances.append(evidence)
            if evidence.instance_id is None:
                # An unidentifiable record cannot be ruled out by instance selection.
                diagnostics.add(Diagnostic.INVALID_METADATA)
    if not matched:
        diagnostics.add(Diagnostic.MODEL_NOT_FOUND)
    eligible = [i for i in instances if instance_id is None or i.instance_id == instance_id]
    for field in ("revision", "selected_variant", "quantization"):
        values = {i.provenance[field] for i in eligible if field in i.provenance}
        if len(values) > 1:
            diagnostics.add(Diagnostic.IDENTITY_CONFLICT)
    return instances, diagnostics


async def discover_lmstudio_context(
    client: httpx.AsyncClient,
    identity: LocalContextIdentity,
    model_id: str,
    instance_id: str | None = None,
) -> LocalContextObservation:
    """Observe exact native keys and instance IDs without loading or generating.

    Callers own the client and route identity. Transport credentials stay on the
    request; diagnostics contain fixed codes, never response bodies or exception
    text. All catalog duplicates remain independent evidence.
    """
    provenance = {"source": _MODELS_PATH}
    diagnostics: set[Diagnostic] = set()
    instances: list[LocalContextInstance] = []
    try:
        url = httpx.URL(identity.api_base)
        path = url.path.rstrip("/")
        for suffix in ("/api/v1", "/v1"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        url = url.copy_with(path=f"{path}{_MODELS_PATH}", query=None, fragment=None)
        provenance["api_base"] = str(url.copy_with(path=path, username=None, password=None)).rstrip(
            "/"
        )
        headers = {"Authorization": f"Bearer {identity.api_key}"} if identity.api_key else {}
        response = await client.get(
            url.copy_with(username=None, password=None),
            headers=headers,
            timeout=_TIMEOUT,
            follow_redirects=False,
        )
        response.raise_for_status()
        instances, diagnostics = _catalog_evidence(response.json(), model_id, instance_id)
    except httpx.HTTPStatusError as exc:
        diagnostics.add(Diagnostic.ENDPOINT_UNAVAILABLE)
        provenance["http_status"] = str(exc.response.status_code)
    except (httpx.RequestError, httpx.InvalidURL):
        diagnostics.add(Diagnostic.ENDPOINT_UNAVAILABLE)
    except ValueError:
        diagnostics.add(Diagnostic.INVALID_METADATA)

    eligible = [i for i in instances if instance_id is None or i.instance_id == instance_id]
    canonical = {i.canonical_limit for i in eligible}
    digests = {i.digest for i in eligible}
    return build_context_observation(
        machine_id=identity.machine_id,
        endpoint_id=identity.endpoint_id,
        configuration_fingerprint=identity.configuration_fingerprint,
        provider="lmstudio",
        model_id=model_id,
        instance_id=instance_id,
        canonical_limit=next(iter(canonical)) if len(canonical) == 1 else None,
        digest=next(iter(digests)) if len(digests) == 1 else None,
        instances=instances,
        provenance=provenance,
        diagnostics=diagnostics,
    )
