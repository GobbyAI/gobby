"""Opt-in Gemini sandbox compatibility runner."""

from __future__ import annotations

import pytest

from .runner import BaseSandboxRunnerTests, GEMINI_SPEC, SandboxRunner

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestGeminiSandboxRunner(BaseSandboxRunnerTests):
    __test__ = True
    runner = SandboxRunner(GEMINI_SPEC)
