"""Read-only context discovery from vLLM's served model catalog."""

from __future__ import annotations

from dataclasses import dataclass

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

_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class VLLMServedRecord:
    """One catalog position, retained even when its metadata is unusable."""

    model_id: str | None
    max_model_len: int | None
    model_id_path: str | None
    max_model_len_path: str
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class VLLMServedCatalog:
    """Parsed records plus catalog-level diagnostics."""

    records: tuple[VLLMServedRecord, ...]
    diagnostics: tuple[Diagnostic, ...] = ()

    def model_ids(self) -> list[str]:
        """Project stable, deduplicated IDs for existing selection callers."""
        ids: list[str] = []
        seen: set[str] = set()
        for record in self.records:
            if record.model_id is not None and record.model_id not in seen:
                ids.append(record.model_id)
                seen.add(record.model_id)
        return ids


def _vllm_origin(api_base: str) -> str:
    """Return ``api_base`` without a trailing ``/v1`` segment or slash.

    Path prefixes are preserved for endpoints mounted behind an ingress.
    """
    base = api_base.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    return base


def vllm_api_base(api_base: str) -> str:
    """Return the canonical ``{origin}/v1`` base for every vLLM wire request.

    Configured bases with or without a trailing ``/v1`` yield exactly one
    version segment for discovery and generation clients.
    """
    return f"{_vllm_origin(api_base)}/v1"


def vllm_models_url(api_base: str) -> str:
    """Return the single ``{origin}/v1/models`` discovery URL."""
    return f"{vllm_api_base(api_base)}/models"


def vllm_health_url(api_base: str) -> str:
    """Return the ``{origin}/health`` probe URL for ``api_base``."""
    return f"{_vllm_origin(api_base)}/health"


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_vllm_served_records(payload: object) -> VLLMServedCatalog:
    """Parse vLLM/OpenAI-compatible model records without collapsing evidence."""
    if not isinstance(payload, dict):
        return VLLMServedCatalog((), (Diagnostic.INVALID_METADATA,))
    raw_records = payload.get("data")
    root = "data"
    if not isinstance(raw_records, list):
        raw_records = payload.get("models")
        root = "models"
    if not isinstance(raw_records, list):
        return VLLMServedCatalog((), (Diagnostic.INVALID_METADATA,))

    records: list[VLLMServedRecord] = []
    for index, raw in enumerate(raw_records):
        path = f"{root}[{index}]"
        diagnostics: set[Diagnostic] = set()
        model_id: str | None = None
        model_id_path: str | None = None
        max_model_len: int | None = None
        if not isinstance(raw, dict):
            diagnostics.add(Diagnostic.INVALID_METADATA)
        else:
            for key in ("id", "model", "name"):
                if (value := _text(raw.get(key))) is not None:
                    model_id = value
                    model_id_path = f"{path}.{key}"
                    break
            if model_id is None:
                diagnostics.add(Diagnostic.INVALID_METADATA)
            raw_limit = raw.get("max_model_len")
            max_model_len = positive_limit(raw_limit)
            if raw_limit is not None and max_model_len is None:
                diagnostics.add(Diagnostic.INVALID_METADATA)
        records.append(
            VLLMServedRecord(
                model_id=model_id,
                max_model_len=max_model_len,
                model_id_path=model_id_path,
                max_model_len_path=f"{path}.max_model_len",
                diagnostics=tuple(sorted(diagnostics)),
            )
        )
    return VLLMServedCatalog(tuple(records))


async def discover_vllm_context(
    client: httpx.AsyncClient,
    identity: LocalContextIdentity,
    model_id: str,
) -> LocalContextObservation:
    """Observe vLLM's verified serving limit without loading or generating."""
    provenance = {"source": "/v1/models"}
    diagnostics: set[Diagnostic] = set()
    instances: list[LocalContextInstance] = []
    catalog: VLLMServedCatalog | None = None
    try:
        url = httpx.URL(vllm_models_url(identity.api_base)).copy_with(
            username=None,
            password=None,
            query=None,
            fragment=None,
        )
        provenance["api_base"] = str(
            httpx.URL(vllm_api_base(identity.api_base)).copy_with(
                username=None,
                password=None,
                query=None,
                fragment=None,
            )
        ).rstrip("/")
        headers = {"Authorization": f"Bearer {identity.api_key}"} if identity.api_key else {}
        response = await client.get(
            url,
            headers=headers,
            timeout=_TIMEOUT,
            follow_redirects=False,
        )
        response.raise_for_status()
        catalog = parse_vllm_served_records(response.json())
    except httpx.HTTPStatusError as exc:
        diagnostics.add(Diagnostic.ENDPOINT_UNAVAILABLE)
        provenance["http_status"] = str(exc.response.status_code)
    except (httpx.RequestError, httpx.InvalidURL):
        diagnostics.add(Diagnostic.ENDPOINT_UNAVAILABLE)
    except ValueError:
        diagnostics.add(Diagnostic.INVALID_METADATA)

    if catalog is not None:
        diagnostics.update(catalog.diagnostics)
        matched = False
        for record in catalog.records:
            if record.model_id is None:
                diagnostics.update(record.diagnostics)
                continue
            if record.model_id != model_id:
                continue
            matched = True
            instances.append(
                LocalContextInstance(
                    model_id=model_id,
                    runtime_limit=record.max_model_len,
                    provenance={
                        "model_id": record.model_id_path or "unknown",
                        "runtime_limit": record.max_model_len_path,
                    },
                    diagnostics=record.diagnostics,
                )
            )
        if not matched:
            diagnostics.add(Diagnostic.MODEL_NOT_FOUND)

    return build_context_observation(
        machine_id=identity.machine_id,
        endpoint_id=identity.endpoint_id,
        configuration_fingerprint=identity.configuration_fingerprint,
        provider="vllm",
        model_id=model_id,
        instances=instances,
        provenance=provenance,
        diagnostics=diagnostics,
    )
