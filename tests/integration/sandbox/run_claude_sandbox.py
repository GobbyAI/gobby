"""Opt-in Claude sandbox compatibility runner."""

from __future__ import annotations

import pytest

from .runner import CLAUDE_SPEC, BaseSandboxRunnerTests, SandboxRunner

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestClaudeSandboxRunner(BaseSandboxRunnerTests):
    __test__ = True
    runner = SandboxRunner(CLAUDE_SPEC)
