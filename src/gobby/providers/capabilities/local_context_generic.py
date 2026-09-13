"""Serving-context discovery for generic loopback OpenAI-compatible endpoints."""

from __future__ import annotations

from ipaddress import ip_address
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

_LIMIT_FIELDS = ("context_length", "context_window", "max_model_len")
_TIMEOUT = 10.0


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _models_endpoint(api_base: str) -> tuple[httpx.URL, str] | None:
    """Return the existing OpenAI-compatible models URL for a loopback base."""
    url = httpx.URL(api_base.strip())
    if not _is_loopback(url.host):
        return None

    clean = url.copy_with(username=None, password=None, query=None, fragment=None)
    path = clean.path.rstrip("/")
    if path.endswith("/v1"):
        models_path = f"{path}/models"
    else:
        models_path = f"{path}/v1/models"
    return clean.copy_with(path=models_path), str(clean).rstrip("/")


def _record_identity(record: dict[str, Any], path: str) -> tuple[str | None, str | None]:
    for key in ("id", "model", "name"):
        if (value := _text(record.get(key))) is not None:
            return value, f"{path}.{key}"
    return None, None


def _record_evidence(
    record: dict[str, Any],
    *,
    model_id: str,
    model_id_path: str,
    path: str,
) -> LocalContextInstance:
    diagnostics: set[Diagnostic] = set()
    supplied_paths: list[str] = []
    limits: list[int] = []
    malformed = False
    for field in _LIMIT_FIELDS:
        raw_limit = record.get(field)
        if raw_limit is None:
            continue
        supplied_paths.append(f"{path}.{field}")
        limit = positive_limit(raw_limit)
        if limit is None:
            malformed = True
        else:
            limits.append(limit)

    if malformed:
        diagnostics.add(Diagnostic.INVALID_METADATA)
    runtime_limit = min(limits) if limits and not malformed else None
    provenance = {"model_id": model_id_path}
    if supplied_paths:
        provenance["runtime_limit"] = ",".join(supplied_paths)
    return LocalContextInstance(
        model_id=model_id,
        runtime_limit=runtime_limit,
        provenance=provenance,
        diagnostics=tuple(sorted(diagnostics)),
    )


def _catalog_evidence(
    payload: object, model_id: str
) -> tuple[list[LocalContextInstance], set[Diagnostic]]:
    diagnostics: set[Diagnostic] = set()
    instances: list[LocalContextInstance] = []
    if not isinstance(payload, dict):
        return instances, {Diagnostic.INVALID_METADATA, Diagnostic.MODEL_NOT_FOUND}

    if "data" in payload:
        records = payload["data"]
        root = "data"
    else:
        records = payload.get("models")
        root = "models"
    if not isinstance(records, list):
        return instances, {Diagnostic.INVALID_METADATA, Diagnostic.MODEL_NOT_FOUND}

    matched = False
    for index, raw_record in enumerate(records):
        path = f"{root}[{index}]"
        if not isinstance(raw_record, dict):
            diagnostics.add(Diagnostic.INVALID_METADATA)
            continue
        record_id, record_id_path = _record_identity(raw_record, path)
        if record_id is None or record_id_path is None:
            diagnostics.add(Diagnostic.INVALID_METADATA)
            continue
        if record_id != model_id:
            continue
        matched = True
        instances.append(
            _record_evidence(
                raw_record,
                model_id=model_id,
                model_id_path=record_id_path,
                path=path,
            )
        )
    if not matched:
        diagnostics.add(Diagnostic.MODEL_NOT_FOUND)
    return instances, diagnostics


async def discover_generic_context(
    client: httpx.AsyncClient,
    identity: LocalContextIdentity,
    model_id: str,
) -> LocalContextObservation:
    """Observe explicit serving limits from a configured loopback model catalog."""
    provenance = {"source": "/v1/models"}
    diagnostics: set[Diagnostic] = set()
    instances: list[LocalContextInstance] = []
    try:
        endpoint = _models_endpoint(identity.api_base)
        if endpoint is None:
            diagnostics.add(Diagnostic.NOT_LOCAL)
        else:
            url, api_base = endpoint
            provenance["api_base"] = api_base
            headers = {"Authorization": f"Bearer {identity.api_key}"} if identity.api_key else {}
            response = await client.get(
                url,
                headers=headers,
                timeout=_TIMEOUT,
                follow_redirects=False,
            )
            response.raise_for_status()
            instances, catalog_diagnostics = _catalog_evidence(response.json(), model_id)
            diagnostics.update(catalog_diagnostics)
    except httpx.HTTPStatusError as exc:
        diagnostics.add(Diagnostic.ENDPOINT_UNAVAILABLE)
        provenance["http_status"] = str(exc.response.status_code)
    except (httpx.RequestError, httpx.InvalidURL):
        diagnostics.add(Diagnostic.ENDPOINT_UNAVAILABLE)
    except ValueError:
        diagnostics.add(Diagnostic.INVALID_METADATA)

    return build_context_observation(
        machine_id=identity.machine_id,
        endpoint_id=identity.endpoint_id,
        configuration_fingerprint=identity.configuration_fingerprint,
        provider="openai-compatible",
        model_id=model_id,
        instances=instances,
        provenance=provenance,
        diagnostics=diagnostics,
    )
