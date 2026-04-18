"""Opt-in Codex sandbox compatibility runner."""

from __future__ import annotations

import pytest

from .runner import CODEX_SPEC, BaseSandboxRunnerTests, SandboxRunner

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestCodexSandboxRunner(BaseSandboxRunnerTests):
    __test__ = True
    runner = SandboxRunner(CODEX_SPEC)
