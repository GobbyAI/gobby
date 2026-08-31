"""Opt-in live probe: AGY as a primary text-gen candidate at feature_low/mid.

Skipped unless ``GOBBY_RUN_AGY_PROBE=1`` (mirrors the ``GOBBY_RUN_AGY_MODELS_LIVE``
drift check in
``tests/providers/capabilities/collectors/test_providers_capabilities_collectors_agy.py``).
It spawns the real ``agy`` CLI, so it is intentionally excluded from the default suite.

Confirms AGY returns usable text and parseable JSON when it is the sole candidate,
that ``reasoning_effort="auto"`` resolves to each base model's catalog default
(``low`` for flash, ``high`` for pro), and that the composed ``--model`` display
string is exact (AGY silently runs the account-default model on an unknown
``--model``, so an exact match is the only correctness signal).
"""

from __future__ import annotations

import os

import pytest

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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("GOBBY_RUN_AGY_PROBE") != "1",
        reason="set GOBBY_RUN_AGY_PROBE=1 to run the live AGY probe",
    ),
]

# tier -> (base model id, exact AGY --model display, resolved auto effort)
_CASES = [
    ("feature_low", "gemini-3.5-flash", "Gemini 3.5 Flash (Medium)", "medium"),
    ("feature_mid", "gemini-3.1-pro", "Gemini 3.1 Pro (High)", "high"),
]


class _ProbeAgyAdapter(AgyCLITextGenerateAdapter):
    """Real AGY adapter that records the composed CLI command for assertions."""

    def __init__(self) -> None:
        super().__init__(timeout_seconds=90.0)
        self.commands: list[list[str]] = []

    def build_command(self, request: TextGenerationRequest) -> list[str]:
        command = super().build_command(request)
        self.commands.append(command)
        return command

    @property
    def last_model_display(self) -> str | None:
        if not self.commands:
            return None
        command = self.commands[-1]
        if "--model" not in command:
            return None
        return command[command.index("--model") + 1]


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


@pytest.fixture
def probe_service(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, _ProbeAgyAdapter]:
    """Build the real text-gen service wired to a command-capturing AGY adapter."""
    adapter = _ProbeAgyAdapter()
    monkeypatch.setattr(
        _text_generation_adapters,
        "AgyCLITextGenerateAdapter",
        lambda **_kwargs: adapter,
    )
    service = build_daemon_text_generation_service(
        DaemonConfig(),
        registry=_agy_only_registry(),
    )
    return service, adapter


@pytest.mark.parametrize(("tier", "model", "display", "effort"), _CASES)
async def test_agy_text_probe(
    probe_service: tuple[object, _ProbeAgyAdapter],
    tier: str,
    model: str,
    display: str,
    effort: str,
) -> None:
    service, adapter = probe_service
    result = await service.generate_result(  # type: ignore[attr-defined]
        _request(f"Reply with one short sentence identifying {tier}.", model)
    )
    assert result.text.strip()
    assert adapter.last_model_display == display
    assert result.applied_reasoning_effort == effort


@pytest.mark.parametrize(("tier", "model", "display", "effort"), _CASES)
async def test_agy_json_probe(
    probe_service: tuple[object, _ProbeAgyAdapter],
    tier: str,
    model: str,
    display: str,
    effort: str,
) -> None:
    service, adapter = probe_service
    result = await service.generate_json(  # type: ignore[attr-defined]
        _request(
            f'Return exactly this JSON object with no prose: {{"ok": true, "tier": "{tier}"}}',
            model,
        )
    )
    assert isinstance(result, dict)
    assert result.get("ok") is True
    assert adapter.last_model_display == display
