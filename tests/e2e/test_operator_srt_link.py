"""Seat-visible outcomes for the managed SRT e2e prerequisite."""

from pathlib import Path

import pytest

from gobby.agents.srt_runtime import SrtRuntimeError
from gobby.utils.dependency_requirements import SRT_RELEASE
from tests.e2e import conftest
from tests.e2e.conftest import link_operator_srt, link_spawning_operator_srt

pytestmark = pytest.mark.unit

SEAT_OUTCOMES = [
    pytest.param(True, pytest.fail.Exception, id="seat-fails"),
    pytest.param(False, pytest.skip.Exception, id="operator-skips"),
]


def _operator_srt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, seat: bool) -> Path:
    operator = tmp_path / "operator"
    monkeypatch.setattr(Path, "home", lambda: operator)
    if seat:
        monkeypatch.setenv("SANDBOX_RUNTIME", "1")
    else:
        monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
    return operator / ".gobby" / "tools" / "srt" / SRT_RELEASE.version


@pytest.mark.parametrize(("seat", "outcome"), SEAT_OUTCOMES)
def test_missing_operator_srt_fails_only_inside_a_seat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seat: bool,
    outcome: type[BaseException],
) -> None:
    _operator_srt(monkeypatch, tmp_path, seat=seat)

    with pytest.raises(outcome, match="is not installed"):
        link_operator_srt(tmp_path / "isolated")


@pytest.mark.parametrize(("seat", "outcome"), SEAT_OUTCOMES)
def test_unverifiable_operator_srt_fails_only_inside_a_seat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seat: bool,
    outcome: type[BaseException],
) -> None:
    _operator_srt(monkeypatch, tmp_path, seat=seat).mkdir(parents=True)

    def denied() -> None:
        raise SrtRuntimeError("receipt unreadable")

    monkeypatch.setattr(conftest, "verify_srt_installation", denied)

    with pytest.raises(outcome, match="failed verification: receipt unreadable"):
        link_operator_srt(tmp_path / "isolated")


def test_verified_operator_srt_links_inside_a_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    operator = _operator_srt(monkeypatch, tmp_path, seat=True)
    operator.mkdir(parents=True)
    monkeypatch.setattr(conftest, "verify_srt_installation", lambda: None)
    isolated = tmp_path / "isolated"

    link_operator_srt(isolated)

    assert (isolated / "tools" / "srt" / SRT_RELEASE.version).resolve() == operator.resolve()


def test_agent_spawning_srt_refuses_seat_nesting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _operator_srt(monkeypatch, tmp_path, seat=True).mkdir(parents=True)
    monkeypatch.setattr(conftest, "verify_srt_installation", lambda: None)
    isolated = tmp_path / "isolated"

    with pytest.raises(pytest.fail.Exception, match="nested sandbox"):
        link_spawning_operator_srt(isolated)

    assert not (isolated / "tools").exists()


def test_agent_spawning_srt_links_outside_a_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    operator = _operator_srt(monkeypatch, tmp_path, seat=False)
    operator.mkdir(parents=True)
    monkeypatch.setattr(conftest, "verify_srt_installation", lambda: None)
    isolated = tmp_path / "isolated"

    link_spawning_operator_srt(isolated)

    assert (isolated / "tools" / "srt" / SRT_RELEASE.version).resolve() == operator.resolve()
