"""Injected release batch ports that record what a batch asked them to do (plan 4.2)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.releases.batch import (
    ActivationFailed,
    ActivationKind,
    BatchEnvironment,
    Check,
    CheckResult,
    latest_baseline,
)
from gobby.releases.smoke import SmokeResult
from gobby.releases.window import ReleaseWindow, acquire_window, read_window
from tests.fixtures.landing_case import LandingCase

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
# ``frontend_lint`` is deliberately unconfigured: the plan omits it.
CONFIGURED = {
    "lint": "lint-cmd",
    "format": "format-cmd",
    "type_check": "type-cmd",
    "cargo_fmt": "fmt-cmd",
    "ts_check": "ts-cmd",
    "frontend_tests": "web-test-cmd",
}


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Fakes:
    """Injected batch ports that record what the batch asked them to do.

    ``during`` runs once on ``loop`` while the first check holds the window, and its result
    lands in ``observed``. The batch records a raised probe as a failed check, so a probe
    reports what it saw and the test asserts after the batch.
    """

    case: LandingCase
    clock: Clock
    fail: str | None = None
    kind: ActivationKind = "cutover"
    loop: asyncio.AbstractEventLoop | None = None
    successor: str | None = None
    during: Callable[[], Coroutine[Any, Any, object]] | None = None
    observed: object = None
    checks: list[tuple[str, str]] = field(default_factory=list)
    activations: list[str] = field(default_factory=list)
    announcements: list[str] = field(default_factory=list)
    baselines_at_smoke: list[str | None] = field(default_factory=list)
    stolen: ReleaseWindow | None = None

    def env(self) -> BatchEnvironment:
        return BatchEnvironment(
            command=CONFIGURED.get,
            run_check=self._run_check,
            activation_kind=lambda: self.kind,
            activate=self._activate,
            smoke=self._smoke,
            announce=self._announce,
            clock=self.clock,
            renew_every=0.01,
        )

    def _run_check(self, check: Check) -> CheckResult:
        self.checks.append((check.name, self.case.git("rev-parse", "HEAD")))
        if self.during is not None:
            probe, self.during = self.during, None
            assert self.loop is not None
            self.observed = asyncio.run_coroutine_threadsafe(probe(), self.loop).result(timeout=120)
        if self.fail == "lease" and self.stolen is None:
            self._steal()
        failed = self.fail == "check" and check.name == "lint"
        return CheckResult(check.name, check.command, not failed, "lint failed" if failed else "")

    def _steal(self) -> None:
        """A successor takes the window after this batch's lease expires."""
        held = read_window(self.case.repo / ".git")
        assert isinstance(held, ReleaseWindow)
        assert self.loop is not None and self.successor is not None
        self.clock.now = held.expires_at + timedelta(seconds=1)
        stolen = acquire_window(
            self.case.db,
            project_id=self.case.project_id,
            owner_session_id=self.successor,
            now=self.clock.now,
        )
        self.stolen = asyncio.run_coroutine_threadsafe(stolen, self.loop).result(timeout=30)

    def _activate(self, kind: ActivationKind) -> None:
        self.activations.append(kind)
        if self.fail in ("build", "restart"):
            raise ActivationFailed(f"{self.fail} failed")

    def _smoke(self) -> list[SmokeResult]:
        self.baselines_at_smoke.append(latest_baseline(self.case.repo / ".git"))
        if self.fail == "late_lease":
            self._steal()
        return [SmokeResult("health", self.fail != "smoke", "probe")]

    def _announce(self, text: str) -> str | None:
        self.announcements.append(text)
        return None
