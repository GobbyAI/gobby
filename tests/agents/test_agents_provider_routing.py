"""Provider routing acceptance tests for manifest-backed detectors."""

from __future__ import annotations

import logging

import pytest

from gobby.agents.detection.matcher import CompiledManifest, compile_manifest
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.stall_classifier import StallClassifier, StallStatus

pytestmark = pytest.mark.unit


class StaticRegistry:
    def __init__(self, manifests: dict[str, str]) -> None:
        self._manifests = {
            provider: compile_manifest(content) for provider, content in manifests.items()
        }

    def for_provider(self, provider_id: str) -> CompiledManifest | None:
        return self._manifests.get(provider_id)


def _manifest(provider: str, marker: str) -> str:
    return f'''\
id = "{provider}"
version = "1"
engine = 1

[[rules]]
id = "trust_prompt"
state = "blocked"
reason = "trust"
priority = 1000
region = "whole_recent"
contains = ["{marker} trust"]

[[rules]]
id = "provider_error"
state = "stall"
reason = "provider_error"
priority = 850
region = "whole_recent"
contains = ["{marker} unavailable"]

[[rules]]
id = "idle_prompt"
state = "idle"
priority = 100
region = "whole_recent"
contains = ["{marker} idle"]
'''


def test_multi_and_unknown_provider(caplog: pytest.LogCaptureFixture) -> None:
    registry = StaticRegistry(
        {
            "claude": _manifest("claude", "alpha"),
            "codex": _manifest("codex", "beta"),
        }
    )

    claude_prompt = PromptDetector(registry, "claude")
    codex_prompt = PromptDetector(registry, "codex")
    claude_idle = IdleDetector(registry, "claude")
    codex_idle = IdleDetector(registry, "codex")
    claude_stall = StallClassifier(registry, "claude")
    codex_stall = StallClassifier(registry, "codex")

    assert claude_prompt.detect_trust_prompt("alpha trust") is True
    assert claude_prompt.detect_trust_prompt("beta trust") is False
    assert codex_prompt.detect_trust_prompt("beta trust") is True
    assert claude_idle.detect("alpha idle") == "idle"
    assert codex_idle.detect("beta idle") == "idle"
    assert claude_stall.is_provider_error("alpha unavailable") is True
    assert codex_stall.is_provider_error("beta unavailable") is True

    caplog.set_level(logging.WARNING)
    unknown_prompt = PromptDetector(registry, "other")
    unknown_idle = IdleDetector(registry, "other")
    unknown_stall = StallClassifier(registry, "other")

    assert unknown_prompt.detect_prompt("alpha trust") is None
    assert unknown_idle.detect("alpha idle") == "unknown"
    assert unknown_stall.classify("run", error="alpha unavailable").status is StallStatus.UNKNOWN
    unknown_prompt.detect_prompt("alpha trust")

    warnings = [
        record
        for record in caplog.records
        if "No detection manifest for provider other" in record.getMessage()
    ]
    assert len(warnings) == 1
