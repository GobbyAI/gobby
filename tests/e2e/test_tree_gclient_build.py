"""A failed tree gclient build returns a report; it never escapes the collection hook."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

import pytest

from tests.e2e import conftest

pytestmark = pytest.mark.unit

_BUILD_OK = subprocess.CompletedProcess(args=["cargo", "build"], returncode=0, stdout="", stderr="")


def _metadata(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["cargo", "metadata"], returncode=0, stdout=stdout, stderr=""
    )


def _runner(outcomes: Sequence[object]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Replay one outcome per subprocess call: a result to return, or an exception to raise."""
    pending = iter(outcomes)

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        outcome = next(pending)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, subprocess.CompletedProcess)
        return outcome

    return run


@pytest.mark.parametrize(
    ("outcomes", "report"),
    [
        pytest.param(
            [subprocess.TimeoutExpired(["cargo", "build"], 2400.0)],
            "timed out",
            id="build-timeout",
        ),
        pytest.param(
            [FileNotFoundError(2, "No such file or directory", "cargo")],
            "could not run cargo",
            id="build-cargo-missing",
        ),
        pytest.param(
            [_BUILD_OK, subprocess.TimeoutExpired(["cargo", "metadata"], 600.0)],
            "cargo metadata timed out",
            id="metadata-timeout",
        ),
        pytest.param(
            [_BUILD_OK, OSError("metadata could not start")],
            "cargo metadata could not run",
            id="metadata-oserror",
        ),
        pytest.param(
            [_BUILD_OK, _metadata("not json")],
            "cargo metadata output is unusable",
            id="metadata-not-json",
        ),
        pytest.param(
            [_BUILD_OK, _metadata("{}")],
            "cargo metadata output is unusable",
            id="metadata-missing-target-directory",
        ),
    ],
)
def test_build_failure_returns_report_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, outcomes: Sequence[object], report: str
) -> None:
    monkeypatch.setattr(subprocess, "run", _runner(outcomes))

    result = conftest.build_tree_gclient.__wrapped__()

    assert isinstance(result, str), result
    assert report in result
