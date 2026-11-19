"""Contract for the deferred Discord adapter's kept artifacts.

Discord ships only if a live test-guild round trip passes, so until an operator names a
guild it is deferred with two artifacts kept:

- ``discord/fixtures/*.json``: provenance-only fixtures taken from the adapter's own unit
  tests. Each carries ``source_file`` (the adapter module), ``source_test`` (a pytest node
  id of a test under ``tests/communications``), ``contract_ref`` (a published https
  Discord API page) and ``oracle: false``.
- ``discord/test_guild_round_trip.py``: the operator-gated script. It exposes
  ``STAGES``, the five stages ``("connect", "inbound", "outbound", "reconnect",
  "attachment")`` in that order; ``run_round_trip(driver)``, which calls
  ``driver.run_stage(stage)`` for each stage in order and returns a mapping of every
  stage to ``"passed"``, ``"failed"`` (the stage raised) or ``"skipped"`` (an earlier
  stage failed, so it was not run); and ``test_guild_round_trip()``, a synchronous entry
  point that skips unless ``DISCORD_TEST_GUILD_ID`` is set and opens no socket before
  that check.
"""

from __future__ import annotations

import importlib
import json
import socket
from pathlib import Path
from types import ModuleType
from typing import NoReturn

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DISCORD_DIR = Path(__file__).resolve().parent / "discord"
FIXTURE_DIR = DISCORD_DIR / "fixtures"
SCRIPT = DISCORD_DIR / "test_guild_round_trip.py"
SCRIPT_MODULE = "tests.contracts.comms.discord.test_guild_round_trip"
GUILD_ENV = "DISCORD_TEST_GUILD_ID"
STAGES = ("connect", "inbound", "outbound", "reconnect", "attachment")
PROVENANCE_KEYS = ("source_file", "source_test", "contract_ref", "oracle")


def _script() -> ModuleType:
    assert SCRIPT.is_file(), f"the Discord test-guild script does not exist at {SCRIPT}"
    return importlib.import_module(SCRIPT_MODULE)


class _OfflineDriver:
    """A driver with no network: every stage it is asked to run fails to connect."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_stage(self, stage: str) -> None:
        self.calls.append(stage)
        raise ConnectionError(f"offline: {stage}")


class _PassingDriver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_stage(self, stage: str) -> None:
        self.calls.append(stage)


def _refuse_connect(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError(f"the gated script opened a socket: {args}")


def test_discord_deferral_artifacts_kept() -> None:
    assert FIXTURE_DIR.is_dir(), f"the Discord fixture directory does not exist at {FIXTURE_DIR}"
    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    assert fixtures, f"{FIXTURE_DIR} holds no fixture"
    for fixture in fixtures:
        document = json.loads(fixture.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{fixture} is not a JSON object"
        missing = [key for key in PROVENANCE_KEYS if key not in document]
        assert missing == [], f"{fixture} lacks the provenance keys {missing}"
        assert document["oracle"] is False, f"{fixture} claims to be an oracle"
        assert (REPO_ROOT / document["source_file"]).is_file(), f"{fixture} source_file"
        test_file = str(document["source_test"]).partition("::")[0]
        assert test_file.startswith("tests/communications/"), f"{fixture} source_test"
        assert (REPO_ROOT / test_file).is_file(), f"{fixture} names a missing test file"
        assert str(document["contract_ref"]).startswith("https://"), f"{fixture} contract_ref"
    script = _script()
    assert tuple(script.STAGES) == STAGES, f"the script runs the stages {script.STAGES}"


def test_discord_round_trip_reports_every_stage() -> None:
    script = _script()
    passing = _PassingDriver()
    assert script.run_round_trip(passing) == dict.fromkeys(STAGES, "passed")
    assert passing.calls == list(STAGES)
    offline = _OfflineDriver()
    outcomes = script.run_round_trip(offline)
    assert outcomes == {
        "connect": "failed",
        "inbound": "skipped",
        "outbound": "skipped",
        "reconnect": "skipped",
        "attachment": "skipped",
    }
    assert offline.calls == ["connect"]


def test_discord_script_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script()
    monkeypatch.delenv(GUILD_ENV, raising=False)
    monkeypatch.setattr(socket, "create_connection", _refuse_connect)
    monkeypatch.setattr(socket.socket, "connect", _refuse_connect)
    with pytest.raises(pytest.skip.Exception) as skipped:
        script.test_guild_round_trip()
    assert GUILD_ENV in str(skipped.value), "the skip does not name the guild variable"
