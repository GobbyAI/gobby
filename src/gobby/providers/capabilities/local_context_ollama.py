"""Metadata-only Ollama context discovery."""

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

_SHOW_PATH = "/api/show"
_PS_PATH = "/api/ps"
_TIMEOUT = 10.0


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _same_model(left: str, right: str) -> bool:
    """Accept exact names plus Ollama's omitted ``:latest`` equivalence."""
    if left == right:
        return True
    left_base = left[: -len(":latest")] if left.endswith(":latest") else left
    right_base = right[: -len(":latest")] if right.endswith(":latest") else right
    return left_base == right_base and (left.endswith(":latest") or right.endswith(":latest"))


def _native_urls(api_base: str) -> tuple[httpx.URL, httpx.URL, str]:
    url = httpx.URL(api_base)
    path = url.path.rstrip("/")
    for suffix in ("/api/v1", "/v1", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    clean = url.copy_with(
        path=path,
        query=None,
        fragment=None,
        username=None,
        password=None,
    )
    return (
        clean.copy_with(path=f"{path}{_SHOW_PATH}"),
        clean.copy_with(path=f"{path}{_PS_PATH}"),
        str(clean).rstrip("/"),
    )


def _digest(value: object, diagnostics: set[Diagnostic]) -> str | None:
    if value is None:
        return None
    digest = _text(value)
    if digest is None:
        diagnostics.add(Diagnostic.INVALID_METADATA)
    return digest


def _show_evidence(
    payload: object,
) -> tuple[int | None, str | None, dict[str, str], set[Diagnostic]]:
    diagnostics: set[Diagnostic] = set()
    provenance: dict[str, str] = {
        "architecture": f"{_SHOW_PATH}.model_info.general.architecture",
    }
    if not isinstance(payload, dict):
        return (
            None,
            None,
            provenance,
            {
                Diagnostic.CANONICAL_UNKNOWN,
                Diagnostic.INVALID_METADATA,
            },
        )

    digest = _digest(payload.get("digest"), diagnostics)
    if digest is not None:
        provenance["digest"] = digest
        provenance["digest_source"] = f"{_SHOW_PATH}.digest"

    model_info = payload.get("model_info")
    if not isinstance(model_info, dict):
        diagnostics.update({Diagnostic.CANONICAL_UNKNOWN, Diagnostic.INVALID_METADATA})
        return None, digest, provenance, diagnostics

    architecture = _text(model_info.get("general.architecture"))
    canonical: int | None = None
    if architecture is None:
        diagnostics.add(Diagnostic.CANONICAL_UNKNOWN)
        if model_info.get("general.architecture") is not None:
            diagnostics.add(Diagnostic.INVALID_METADATA)
    else:
        canonical_path = f"{_SHOW_PATH}.model_info.{architecture}.context_length"
        provenance["canonical_limit"] = canonical_path
        raw_canonical = model_info.get(f"{architecture}.context_length")
        canonical = positive_limit(raw_canonical)
        if canonical is None:
            diagnostics.add(Diagnostic.CANONICAL_UNKNOWN)
            if raw_canonical is not None:
                diagnostics.add(Diagnostic.INVALID_METADATA)
    return canonical, digest, provenance, diagnostics


def _record_names(record: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (field, value)
        for field in ("model", "name")
        if (value := _text(record.get(field))) is not None
    ]


def _running_instance(
    record: dict[str, Any],
    index: int,
    model_id: str,
    canonical: int | None,
    show_digest: str | None,
    show_provenance: dict[str, str],
) -> LocalContextInstance | None:
    names = _record_names(record)
    matching_names = [(field, name) for field, name in names if _same_model(name, model_id)]
    if not matching_names:
        return None

    path = f"{_PS_PATH}.models[{index}]"
    diagnostics: set[Diagnostic] = set()
    for field in ("model", "name"):
        if record.get(field) is not None and _text(record.get(field)) is None:
            diagnostics.add(Diagnostic.INVALID_METADATA)
    if any(not _same_model(name, matching_names[0][1]) for _, name in names):
        diagnostics.add(Diagnostic.IDENTITY_CONFLICT)

    raw_runtime = record.get("context_length")
    runtime = positive_limit(raw_runtime)
    if runtime is None:
        diagnostics.add(Diagnostic.RUNTIME_UNKNOWN)
        if raw_runtime is not None:
            diagnostics.add(Diagnostic.INVALID_METADATA)
    if canonical is None:
        diagnostics.add(Diagnostic.CANONICAL_UNKNOWN)

    runtime_digest = _digest(record.get("digest"), diagnostics)
    digest = runtime_digest or show_digest
    if runtime_digest is not None and show_digest is not None and runtime_digest != show_digest:
        diagnostics.add(Diagnostic.IDENTITY_CONFLICT)

    name_field, native_model_id = matching_names[0]
    provenance = {
        "model_id": f"{path}.{name_field}",
        "native_model_id": native_model_id,
        "runtime_limit": f"{path}.context_length",
    }
    if canonical_path := show_provenance.get("canonical_limit"):
        provenance["canonical_limit"] = canonical_path
    if architecture_path := show_provenance.get("architecture"):
        provenance["architecture"] = architecture_path
    if show_digest is not None:
        provenance["show_digest"] = show_digest
        provenance["show_digest_source"] = f"{_SHOW_PATH}.digest"
    if runtime_digest is not None:
        provenance["runtime_digest"] = runtime_digest
        provenance["runtime_digest_source"] = f"{path}.digest"
        provenance["digest"] = runtime_digest
        provenance["digest_source"] = f"{path}.digest"
    elif show_digest is not None:
        provenance["digest"] = show_digest
        provenance["digest_source"] = f"{_SHOW_PATH}.digest"

    return LocalContextInstance(
        model_id=model_id,
        digest=digest,
        canonical_limit=canonical,
        runtime_limit=runtime,
        provenance=provenance,
        diagnostics=tuple(diagnostics),
    )


def _running_evidence(
    payload: object,
    model_id: str,
    canonical: int | None,
    show_digest: str | None,
    show_provenance: dict[str, str],
) -> tuple[list[LocalContextInstance], set[Diagnostic]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return [], {Diagnostic.INVALID_METADATA}

    instances: list[LocalContextInstance] = []
    for index, record in enumerate(payload["models"]):
        if not isinstance(record, dict):
            continue
        instance = _running_instance(
            record,
            index,
            model_id,
            canonical,
            show_digest,
            show_provenance,
        )
        if instance is not None:
            instances.append(instance)
    diagnostics: set[Diagnostic] = set()
    if not instances:
        diagnostics.add(Diagnostic.MODEL_NOT_FOUND)
    return instances, diagnostics


async def discover_ollama_context(
    client: httpx.AsyncClient,
    identity: LocalContextIdentity,
    model_id: str,
) -> LocalContextObservation:
    """Observe Ollama show/ps metadata without loading, generating, or mutating models."""
    diagnostics: set[Diagnostic] = set()
    provenance: dict[str, str] = {"show_source": _SHOW_PATH, "runtime_source": _PS_PATH}
    canonical: int | None = None
    show_digest: str | None = None
    show_provenance: dict[str, str] = {}
    instances: list[LocalContextInstance] = []

    try:
        show_url, ps_url, clean_base = _native_urls(identity.api_base)
        provenance["api_base"] = clean_base
        headers = {"Authorization": f"Bearer {identity.api_key}"} if identity.api_key else {}
        show_response = await client.post(
            show_url,
            headers=headers,
            json={"model": model_id},
            timeout=_TIMEOUT,
            follow_redirects=False,
        )
        show_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        diagnostics.add(Diagnostic.ENDPOINT_UNAVAILABLE)
        provenance["show_http_status"] = str(exc.response.status_code)
    except (httpx.RequestError, httpx.InvalidURL):
        diagnostics.add(Diagnostic.ENDPOINT_UNAVAILABLE)
    else:
        try:
            canonical, show_digest, show_provenance, show_diagnostics = _show_evidence(
                show_response.json()
            )
            provenance.update(show_provenance)
            diagnostics.update(show_diagnostics)
        except ValueError:
            diagnostics.update({Diagnostic.CANONICAL_UNKNOWN, Diagnostic.INVALID_METADATA})

        try:
            ps_response = await client.get(
                ps_url,
                headers=headers,
                timeout=_TIMEOUT,
                follow_redirects=False,
            )
            ps_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            diagnostics.add(Diagnostic.ENDPOINT_UNAVAILABLE)
            provenance["runtime_http_status"] = str(exc.response.status_code)
        except httpx.RequestError:
            diagnostics.add(Diagnostic.ENDPOINT_UNAVAILABLE)
        else:
            try:
                instances, running_diagnostics = _running_evidence(
                    ps_response.json(),
                    model_id,
                    canonical,
                    show_digest,
                    show_provenance,
                )
                diagnostics.update(running_diagnostics)
            except ValueError:
                diagnostics.add(Diagnostic.INVALID_METADATA)

    instance_digests = {instance.digest for instance in instances if instance.digest is not None}
    digest = show_digest
    if digest is None and len(instance_digests) == 1:
        digest = next(iter(instance_digests))
    return build_context_observation(
        machine_id=identity.machine_id,
        endpoint_id=identity.endpoint_id,
        configuration_fingerprint=identity.configuration_fingerprint,
        provider="ollama",
        model_id=model_id,
        canonical_limit=canonical,
        digest=digest,
        instances=instances,
        provenance=provenance,
        diagnostics=diagnostics,
    )
