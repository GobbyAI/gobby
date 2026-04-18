"""Opt-in Gemini sandbox compatibility runner."""

from __future__ import annotations

import pytest

from .runner import GEMINI_SPEC, BaseSandboxRunnerTests, SandboxRunner

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestGeminiSandboxRunner(BaseSandboxRunnerTests):
    __test__ = True
    runner: SandboxRunner = SandboxRunner(GEMINI_SPEC)
