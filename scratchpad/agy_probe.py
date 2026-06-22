from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from gobby.ai import (
    AgyCLITextGenerateAdapter,
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    TextGenerationRequest,
    _text_generation_adapters,
    build_daemon_text_generation_service,
)
from gobby.config.app import DaemonConfig
from gobby.config.feature_base import FeatureCandidateConfig


@dataclass
class ProbeRecord:
    name: str
    mode: str
    ok: bool
    latency_ms: int
    resolved_effort: str | None
    composed_model: str | None
    output: str
    json_valid: bool | None = None


class ProbeAgyAdapter(AgyCLITextGenerateAdapter):
    def __init__(self) -> None:
        super().__init__(timeout_seconds=90.0)
        self.commands: list[list[str]] = []
        self.requests: list[TextGenerationRequest] = []

    def build_command(self, request: TextGenerationRequest) -> list[str]:
        command = super().build_command(request)
        self.commands.append(command)
        return command

    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        return await super().generate(request)

    @property
    def last_model_display(self) -> str | None:
        if not self.commands:
            return None
        command = self.commands[-1]
        if "--model" not in command:
            return None
        return command[command.index("--model") + 1]

    @property
    def last_resolved_effort(self) -> str | None:
        if not self.requests:
            return None
        return self.requests[-1].reasoning_effort


def _agy_only_registry() -> AICapabilityRegistry:
    return AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="agy",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("gemini-3.5-flash", "gemini-3.1-pro"),
                strict_models=True,
            )
        ]
    )


def _request(prompt: str, model: str) -> TextGenerationRequest:
    return TextGenerationRequest(
        prompt=prompt,
        candidates=(FeatureCandidateConfig(candidate=f"agy/{model}"),),
        reasoning_effort="auto",
        caller="agy-probe",
    )


def _record_failure(name: str, mode: str, start: float, exc: BaseException) -> ProbeRecord:
    return ProbeRecord(
        name=name,
        mode=mode,
        ok=False,
        latency_ms=int((time.perf_counter() - start) * 1000),
        resolved_effort=None,
        composed_model=None,
        output=f"{type(exc).__name__}: {exc}"[:500],
        json_valid=False if mode == "json" else None,
    )


async def _run_text_case(
    service: Any,
    adapter: ProbeAgyAdapter,
    name: str,
    model: str,
) -> ProbeRecord:
    start = time.perf_counter()
    try:
        result = await service.generate_result(
            _request(f"Reply with one short sentence identifying {name}.", model)
        )
    except Exception as exc:
        return _record_failure(name, "text", start, exc)

    return ProbeRecord(
        name=name,
        mode="text",
        ok=True,
        latency_ms=int((time.perf_counter() - start) * 1000),
        resolved_effort=result.applied_reasoning_effort,
        composed_model=adapter.last_model_display,
        output=result.text[:500],
    )


async def _run_json_case(
    service: Any,
    adapter: ProbeAgyAdapter,
    name: str,
    model: str,
) -> ProbeRecord:
    start = time.perf_counter()
    try:
        result = await service.generate_json(
            _request(
                f'Return exactly this JSON object with no prose: {{"ok": true, "tier": "{name}"}}',
                model,
            )
        )
    except Exception as exc:
        return _record_failure(name, "json", start, exc)

    return ProbeRecord(
        name=name,
        mode="json",
        ok=True,
        latency_ms=int((time.perf_counter() - start) * 1000),
        resolved_effort=adapter.last_resolved_effort,
        composed_model=adapter.last_model_display,
        output=json.dumps(result, sort_keys=True)[:500],
        json_valid=isinstance(result, dict),
    )


async def main() -> None:
    adapter = ProbeAgyAdapter()
    original_adapter = _text_generation_adapters.AgyCLITextGenerateAdapter
    records: list[ProbeRecord] = []
    _text_generation_adapters.AgyCLITextGenerateAdapter = lambda **_kwargs: adapter
    try:
        service = build_daemon_text_generation_service(
            DaemonConfig(),
            registry=_agy_only_registry(),
        )

        cases = (
            ("feature_low", "gemini-3.5-flash"),
            ("feature_mid", "gemini-3.1-pro"),
        )
        for name, model in cases:
            records.append(await _run_text_case(service, adapter, name, model))
            records.append(await _run_json_case(service, adapter, name, model))
    finally:
        _text_generation_adapters.AgyCLITextGenerateAdapter = original_adapter

    for record in records:
        print(json.dumps(asdict(record), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
