"""Opt-in Qwen sandbox compatibility runner."""

from __future__ import annotations

import pytest

from .runner import BaseSandboxRunnerTests, QWEN_SPEC, SandboxRunner

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestQwenSandboxRunner(BaseSandboxRunnerTests):
    __test__ = True
    runner = SandboxRunner(QWEN_SPEC)
