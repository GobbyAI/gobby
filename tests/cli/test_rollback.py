"""Tests for the rollback command group.

Each test runs the top-level CLI against a real generation store under tmp_path, with
GOBBY_NATIVE_BIN_DIR pointing at that store. The restore tests replace only the
daemon-facing core: run_restore is a recording fake, and the restart and stop admissions
are stubs. Every hold clear passes --path to a temporary directory, so no test reads or
clears the checkout that runs the suite.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import click
import pytest
from click.testing import CliRunner, Result

from gobby.cli import cli
from gobby.install.bin_generations import (
    Generation,
    GenerationStoreError,
    hold_status,
    read_state,
    retain_outgoing,
    set_hold,
)
from gobby.tasks import landing_policy
from gobby.utils.local_token import GOBBY_AGENT_API_TOKEN_ENV
from gobby.utils.native_bin import NATIVE_BIN_DIR_ENV, native_bin_name

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
FUTURE = datetime(2099, 6, 15, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2100, 6, 15, 12, 0, 0, tzinfo=UTC)
FUTURE_TEXT = "2099-06-15T12:00:00+00:00"
LATER_TEXT = "2100-06-15T12:00:00+00:00"
SCHEMA_IDENTITY = {
    "runner_protocol": 3,
    "baseline_version": 1,
    "baseline_checksum": "a" * 64,
    "latest_version": 468,
    "latest_checksum": "b" * 64,
    "assets_root_hash": "c" * 64,
}
SPAWN_MARKERS = (
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_MANAGED_EXECUTION_ID",
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
    GOBBY_AGENT_API_TOKEN_ENV,
)
NODE_BOOTSTRAP = (
    "datastore_mode: remote\n"
    "hub_daemon_url: http://hub.example.test:60887\n"
    "database_url: postgresql://gobby:secret@100.64.0.10:5432/gobby\n"
)
NODE_REFUSAL = "datastore_mode: remote is a node; a node runs no Python daemon"


def _rollback() -> ModuleType:
    return importlib.import_module("gobby.cli.rollback")


def _restore() -> ModuleType:
    return importlib.import_module("gobby.install.bin_restore")


def _cutover() -> ModuleType:
    return importlib.import_module("gobby.cli.cutover")


def _store() -> ModuleType:
    return importlib.import_module("gobby.install.bin_generations")


def _instant(value: object) -> datetime:
    """An aware UTC instant from a stored ISO-8601 string; a naive one is taken as UTC."""
    assert isinstance(value, str), value
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _run(*args: str) -> Result:
    return CliRunner().invoke(cli, ["rollback", *args])


@pytest.fixture
def bin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty managed bin dir, with no spawn marker, session id or operator home."""
    path = tmp_path / "bin"
    path.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(NATIVE_BIN_DIR_ENV, str(path))
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.delenv("GOBBY_SESSION_ID", raising=False)
    for name in SPAWN_MARKERS:
        monkeypatch.delenv(name, raising=False)
    return path


def _seed(bin_dir: Path, name: str, payload: bytes, version: str | None) -> None:
    """Install a managed binary with the receipt a real promotion leaves beside it."""
    binary = bin_dir / native_bin_name(name)
    binary.write_bytes(payload)
    binary.chmod(0o755)
    receipt = {
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "source_identity": f"src-{name}",
        "version": version,
    }
    (bin_dir / f".{name}-release-identity.json").write_text(json.dumps(receipt), encoding="utf-8")


def _publish(bin_dir: Path, *members: tuple[str, bytes, str | None]) -> Generation:
    """Install each member (name, bytes, version), then retain the installed set."""
    for name, payload, version in members:
        _seed(bin_dir, name, payload, version)
    generation = retain_outgoing(bin_dir, [name for name, _, _ in members], now=NOW)
    assert generation is not None
    return generation


def _plain_dir(tmp_path: Path) -> Path:
    """A directory outside any git checkout, for hold clear calls that must not touch one."""
    plain = tmp_path / "plain"
    plain.mkdir(exist_ok=True)
    return plain


def _git_checkout(path: Path) -> Path:
    """Create a git checkout; returns its common dir, where the landing freeze lives."""
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path / ".git"


def test_rollback_group_is_registered_on_the_top_level_cli() -> None:
    assert "rollback" in cli.commands
    assert cli.commands["rollback"] is _rollback().rollback


def test_rollback_help_lists_every_subcommand() -> None:
    result = CliRunner().invoke(cli, ["rollback", "--help"])

    assert result.exit_code == 0, result.output
    for name in ("list", "pin", "unpin", "hold", "burn", "restore"):
        assert re.search(rf"^\s+{name}\b", result.output, re.MULTILINE), name


@pytest.mark.usefixtures("bin_dir")
def test_list_reports_that_there_are_no_generations() -> None:
    result = _run("list")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert re.search(r"\bno\b|\bnone\b", lines[0], re.IGNORECASE)


@pytest.mark.usefixtures("bin_dir")
def test_list_json_is_an_empty_array_without_generations() -> None:
    result = _run("list", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_list_prints_one_line_per_generation_oldest_first(bin_dir: Path) -> None:
    first = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    second = _publish(bin_dir, ("gdaemon", b"build-two", "1.1.0"))

    result = _run("list")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert first.id in lines[0]
    assert "1.0.0" in lines[0]
    assert second.id in lines[1]
    assert "1.1.0" in lines[1]


def test_list_marks_the_pinned_and_installed_generations_on_their_lines(bin_dir: Path) -> None:
    pinned = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    _publish(bin_dir, ("gdaemon", b"build-two", "1.1.0"))
    _store().pin_generation(bin_dir, pinned.id, until=FUTURE, now=NOW)

    lines = _run("list").stdout.splitlines()

    assert "pinned" in lines[0]
    assert "installed" not in lines[0]
    assert "installed" in lines[1]
    assert "pinned" not in lines[1]


def test_list_json_describes_each_generation_with_the_documented_keys(bin_dir: Path) -> None:
    generation = _publish(
        bin_dir,
        ("gdaemon", b"build-one", "1.0.0"),
        ("ghook", b"hook-one", "2.0.0"),
    )

    result = _run("list", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {
            "id": generation.id,
            "seq": generation.seq,
            "created_at": generation.manifest["created_at"],
            "members": {"gdaemon": "1.0.0", "ghook": "2.0.0"},
            "pinned": False,
            "held": False,
            "installed": True,
        }
    ]


def test_list_json_marks_a_pinned_generation(bin_dir: Path) -> None:
    first = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    second = _publish(bin_dir, ("gdaemon", b"build-two", "1.1.0"))
    _store().pin_generation(bin_dir, first.id, until=FUTURE, now=NOW)

    rows: list[dict[str, Any]] = json.loads(_run("list", "--json").stdout)

    assert [(row["id"], row["pinned"]) for row in rows] == [(first.id, True), (second.id, False)]


def test_list_json_marks_the_generations_from_the_hold_onward_as_held(bin_dir: Path) -> None:
    first = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    second = _publish(bin_dir, ("gdaemon", b"build-two", "1.1.0"))
    set_hold(bin_dir, task_id="t-1", session_id="s-1", expires_at=FUTURE, now=NOW)

    rows: list[dict[str, Any]] = json.loads(_run("list", "--json").stdout)

    assert [(row["id"], row["held"]) for row in rows] == [(first.id, False), (second.id, True)]


def test_list_json_marks_only_the_generation_matching_the_installed_gdaemon(
    bin_dir: Path,
) -> None:
    first = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    second = _publish(bin_dir, ("gdaemon", b"build-two", "1.1.0"))

    rows: list[dict[str, Any]] = json.loads(_run("list", "--json").stdout)

    assert [(row["id"], row["installed"]) for row in rows] == [(first.id, False), (second.id, True)]


def test_pin_by_sequence_number_stores_the_pin(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))

    result = _run("pin", str(generation.seq), "--until", FUTURE_TEXT)

    assert result.exit_code == 0, result.output
    assert _instant(read_state(bin_dir)["pins"][generation.id]["until"]) == FUTURE


def test_pin_names_the_generation_and_its_expiry(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))

    result = _run("pin", str(generation.seq), "--until", FUTURE_TEXT)

    assert result.exit_code == 0, result.output
    assert generation.id in result.output
    assert "2099" in result.output


def test_pin_accepts_a_generation_id(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))

    result = _run("pin", generation.id, "--until", FUTURE_TEXT)

    assert result.exit_code == 0, result.output
    assert generation.id in read_state(bin_dir)["pins"]


def test_pin_relative_until_expires_two_hours_from_now(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    before = datetime.now(UTC)

    result = _run("pin", str(generation.seq), "--until", "+2h")
    after = datetime.now(UTC)

    assert result.exit_code == 0, result.output
    expiry = _instant(read_state(bin_dir)["pins"][generation.id]["until"])
    assert before + timedelta(hours=2) <= expiry <= after + timedelta(hours=2)


def test_pin_relative_minutes_expire_thirty_minutes_from_now(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    before = datetime.now(UTC)

    result = _run("pin", str(generation.seq), "--until", "+30m")
    after = datetime.now(UTC)

    assert result.exit_code == 0, result.output
    expiry = _instant(read_state(bin_dir)["pins"][generation.id]["until"])
    assert before + timedelta(minutes=30) <= expiry <= after + timedelta(minutes=30)


def test_pin_records_the_task_that_holds_it(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))

    result = _run("pin", str(generation.seq), "--until", FUTURE_TEXT, "--task", "t-9")

    assert result.exit_code == 0, result.output
    assert read_state(bin_dir)["pins"][generation.id]["task_id"] == "t-9"


def test_pin_refuses_a_generation_that_is_already_pinned(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    _store().pin_generation(bin_dir, generation.id, until=FUTURE, now=NOW)
    with pytest.raises(GenerationStoreError, match="already pinned") as refusal:
        _store().pin_generation(bin_dir, generation.id, until=LATER, now=NOW)

    result = _run("pin", str(generation.seq), "--until", LATER_TEXT)

    assert result.exit_code != 0
    assert str(refusal.value) in result.output


@pytest.mark.usefixtures("bin_dir")
def test_pin_names_an_unknown_generation_and_exits_nonzero() -> None:
    result = _run("pin", "99", "--until", FUTURE_TEXT)

    assert result.exit_code != 0
    assert "99" in result.output


def test_pin_extend_moves_an_existing_pin_to_the_new_expiry(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    _store().pin_generation(bin_dir, generation.id, until=FUTURE, now=NOW)

    result = _run("pin", "extend", generation.id, "--until", LATER_TEXT)

    assert result.exit_code == 0, result.output
    assert _instant(read_state(bin_dir)["pins"][generation.id]["until"]) == LATER


def test_pin_extend_relative_until_expires_four_hours_from_now(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    one_hour = datetime.now(UTC) + timedelta(hours=1)
    _store().pin_generation(bin_dir, generation.id, until=one_hour)
    before = datetime.now(UTC)

    result = _run("pin", "extend", generation.id, "--until", "+4h")
    after = datetime.now(UTC)

    assert result.exit_code == 0, result.output
    expiry = _instant(read_state(bin_dir)["pins"][generation.id]["until"])
    assert before + timedelta(hours=4) <= expiry <= after + timedelta(hours=4)


def test_unpin_removes_the_pin(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    _store().pin_generation(bin_dir, generation.id, until=FUTURE, now=NOW)

    result = _run("unpin", generation.id)

    assert result.exit_code == 0, result.output
    assert generation.id not in read_state(bin_dir).get("pins", {})


def test_unpin_reports_whether_a_pin_was_removed(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    _store().pin_generation(bin_dir, generation.id, until=FUTURE, now=NOW)
    removed = _run("unpin", generation.id)

    nothing_to_remove = _run("unpin", generation.id)

    assert removed.exit_code == 0, removed.output
    assert nothing_to_remove.exit_code == 0, nothing_to_remove.output
    assert nothing_to_remove.output != removed.output


def test_hold_set_prints_the_token_on_its_own_line_and_stores_it(bin_dir: Path) -> None:
    result = _run("hold", "set", "--task", "t-1", "--until", FUTURE_TEXT)

    assert result.exit_code == 0, result.output
    stored: str = read_state(bin_dir)["hold"]["token"]
    assert stored
    assert f"hold token: {stored}" in result.stdout.splitlines()


def test_hold_set_requires_a_task(bin_dir: Path) -> None:
    result = _run("hold", "set", "--until", FUTURE_TEXT)

    assert result.exit_code == 2
    assert "--task" in result.output
    assert hold_status(bin_dir) is None


def test_hold_set_takes_its_session_from_the_environment(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_SESSION_ID", "sess-env")

    result = _run("hold", "set", "--task", "t-1", "--until", FUTURE_TEXT)

    assert result.exit_code == 0, result.output
    assert read_state(bin_dir)["hold"]["session_id"] == "sess-env"


def test_hold_set_defaults_the_session_to_operator(bin_dir: Path) -> None:
    result = _run("hold", "set", "--task", "t-1", "--until", FUTURE_TEXT)

    assert result.exit_code == 0, result.output
    assert read_state(bin_dir)["hold"]["session_id"] == "operator"


def test_hold_set_takes_an_explicit_session_over_the_environment(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_SESSION_ID", "sess-env")

    result = _run("hold", "set", "--task", "t-1", "--session", "sess-flag", "--until", FUTURE_TEXT)

    assert result.exit_code == 0, result.output
    assert read_state(bin_dir)["hold"]["session_id"] == "sess-flag"


def test_hold_set_refuses_while_another_hold_is_unexpired(bin_dir: Path) -> None:
    first = _run("hold", "set", "--task", "t-1", "--until", FUTURE_TEXT)
    assert first.exit_code == 0, first.output

    second = _run("hold", "set", "--task", "t-2", "--until", FUTURE_TEXT)

    assert second.exit_code != 0
    assert "hold is already set by task t-1" in second.output
    assert "hold token:" not in second.stdout


def test_hold_set_relative_until_expires_two_hours_from_now(bin_dir: Path) -> None:
    before = datetime.now(UTC)

    result = _run("hold", "set", "--task", "t-1", "--until", "+2h")
    after = datetime.now(UTC)

    assert result.exit_code == 0, result.output
    expiry = _instant(read_state(bin_dir)["hold"]["expires_at"])
    assert before + timedelta(hours=2) <= expiry <= after + timedelta(hours=2)


def test_hold_set_reads_a_naive_until_as_local_time(bin_dir: Path) -> None:
    result = _run("hold", "set", "--task", "t-1", "--until", "2099-01-01T00:00:00")

    assert result.exit_code == 0, result.output
    expected = datetime(2099, 1, 1).astimezone(UTC)
    assert _instant(read_state(bin_dir)["hold"]["expires_at"]) == expected


@pytest.mark.parametrize("until", ["soon", "+2d", "+h"])
def test_until_rejects_times_that_are_neither_iso_nor_relative_minutes_or_hours(
    bin_dir: Path, until: str
) -> None:
    result = _run("hold", "set", "--task", "t-1", "--until", until)

    assert result.exit_code == 2
    assert "--until" in result.output
    assert hold_status(bin_dir) is None


def test_hold_extend_moves_the_expiry_for_the_token_holder(bin_dir: Path) -> None:
    token = set_hold(bin_dir, task_id="t-1", session_id="s-1", expires_at=FUTURE, now=NOW)

    result = _run("hold", "extend", "--hold-token", token, "--until", LATER_TEXT)

    assert result.exit_code == 0, result.output
    assert _instant(read_state(bin_dir)["hold"]["expires_at"]) == LATER


def test_hold_extend_refuses_a_token_that_is_not_the_holders(bin_dir: Path) -> None:
    set_hold(bin_dir, task_id="t-1", session_id="s-1", expires_at=FUTURE, now=NOW)

    result = _run("hold", "extend", "--hold-token", "not-the-token", "--until", LATER_TEXT)

    assert result.exit_code != 0
    assert "generation hold token does not match" in result.output
    assert _instant(read_state(bin_dir)["hold"]["expires_at"]) == FUTURE


def test_hold_status_says_when_no_hold_is_stored(bin_dir: Path) -> None:
    result = _run("hold", "status")

    assert result.exit_code == 0, result.output
    assert re.search(r"\bno\b.*\bhold\b", result.output, re.IGNORECASE)


def test_hold_status_never_prints_the_token(bin_dir: Path) -> None:
    token = set_hold(bin_dir, task_id="t-1", session_id="s-1", expires_at=FUTURE, now=NOW)

    result = _run("hold", "status")

    assert result.exit_code == 0, result.output
    assert "t-1" in result.output
    assert token not in result.output


def test_hold_status_json_omits_the_token(bin_dir: Path) -> None:
    token = set_hold(bin_dir, task_id="t-1", session_id="s-1", expires_at=FUTURE, now=NOW)

    result = _run("hold", "status", "--json")

    assert result.exit_code == 0, result.output
    body: dict[str, Any] = json.loads(result.stdout)
    assert isinstance(body, dict)
    assert "token" not in body
    assert body["task_id"] == "t-1"
    assert token not in result.stdout


def test_hold_clear_removes_the_stored_hold(bin_dir: Path, tmp_path: Path) -> None:
    set_hold(bin_dir, task_id="t-1", session_id="s-1", expires_at=FUTURE, now=NOW)

    result = _run("hold", "clear", "--path", str(_plain_dir(tmp_path)))

    assert result.exit_code == 0, result.output
    assert hold_status(bin_dir) is None


def test_hold_clear_says_whether_a_hold_was_stored(bin_dir: Path, tmp_path: Path) -> None:
    plain = str(_plain_dir(tmp_path))
    set_hold(bin_dir, task_id="t-1", session_id="s-1", expires_at=FUTURE, now=NOW)
    cleared = _run("hold", "clear", "--path", plain)

    nothing_stored = _run("hold", "clear", "--path", plain)

    assert cleared.exit_code == 0, cleared.output
    assert nothing_stored.exit_code == 0, nothing_stored.output
    assert nothing_stored.output != cleared.output


@pytest.mark.usefixtures("bin_dir")
def test_hold_clear_turns_off_a_task_bound_freeze_even_without_a_hold(tmp_path: Path) -> None:
    common = _git_checkout(tmp_path / "repo")
    landing_policy.write_freeze(
        common, on=True, reason="cutover in progress", session_id="s-1", task_id="t-1"
    )

    result = _run("hold", "clear", "--path", str(tmp_path / "repo"))

    assert result.exit_code == 0, result.output
    assert landing_policy.read_freeze(common).on is False


@pytest.mark.usefixtures("bin_dir")
def test_hold_clear_leaves_an_unbound_freeze_alone(tmp_path: Path) -> None:
    common = _git_checkout(tmp_path / "repo")
    landing_policy.write_freeze(common, on=True, reason="manual freeze", session_id="s-1")

    result = _run("hold", "clear", "--path", str(tmp_path / "repo"))

    assert result.exit_code == 0, result.output
    assert landing_policy.read_freeze(common).on is True


@pytest.mark.usefixtures("bin_dir")
def test_hold_clear_reads_the_checkout_from_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = _git_checkout(tmp_path / "repo")
    landing_policy.write_freeze(
        common, on=True, reason="cutover in progress", session_id="s-1", task_id="t-1"
    )
    monkeypatch.chdir(tmp_path / "repo")

    result = _run("hold", "clear")

    assert result.exit_code == 0, result.output
    assert landing_policy.read_freeze(common).on is False


def test_hold_clear_outside_a_checkout_clears_the_hold_and_says_no_freeze_was_checked(
    bin_dir: Path, tmp_path: Path
) -> None:
    set_hold(bin_dir, task_id="t-1", session_id="s-1", expires_at=FUTURE, now=NOW)

    result = _run("hold", "clear", "--path", str(_plain_dir(tmp_path)))

    assert result.exit_code == 0, result.output
    assert hold_status(bin_dir) is None
    assert "no checkout freeze was checked" in result.output


def test_burn_records_the_generations_gdaemon_version_as_burned(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))

    result = _run("burn", generation.id)

    assert result.exit_code == 0, result.output
    assert read_state(bin_dir)["burned"]["gdaemon"]["1.0.0"]["generation"] == generation.id


def test_burn_names_the_binary_given_with_bin(bin_dir: Path) -> None:
    generation = _publish(
        bin_dir,
        ("gdaemon", b"build-one", "1.0.0"),
        ("ghook", b"hook-one", "2.0.0"),
    )

    result = _run("burn", str(generation.seq), "--bin", "ghook")

    assert result.exit_code == 0, result.output
    burned: dict[str, Any] = read_state(bin_dir)["burned"]
    assert burned["ghook"]["2.0.0"]["generation"] == generation.id
    assert "gdaemon" not in burned


def test_burn_refuses_a_binary_the_generation_does_not_record(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))

    result = _run("burn", generation.id, "--bin", "ghook")

    assert result.exit_code != 0
    assert "ghook" in result.output
    assert "burned" not in read_state(bin_dir)


def test_burn_refuses_a_generation_that_records_no_version(bin_dir: Path) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", None))

    result = _run("burn", generation.id)

    assert result.exit_code != 0
    assert "gdaemon" in result.output
    assert "burned" not in read_state(bin_dir)


class _RestoreSpy:
    """Stands in for run_restore: records each call, and can probe its start refusal."""

    def __init__(
        self,
        *,
        outcome: object = None,
        error: Exception | None = None,
        probe: Path | None = None,
    ) -> None:
        self.outcome = outcome
        self.error = error
        self.probe = probe
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, bin_dir: Path, generation: Generation, members: list[str], **kwargs: Any
    ) -> object:
        call: dict[str, Any] = {
            "bin_dir": bin_dir,
            "generation": generation,
            "members": list(members),
            **kwargs,
        }
        if self.probe is not None:
            call["probed_start_refusal"] = kwargs["start_refusal"](self.probe)
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        return self.outcome


def _outcome(generation: Generation, aside: Path) -> object:
    outcome_type: Callable[..., object] = _restore().RestoreOutcome
    return outcome_type(
        generation=generation.id,
        restored={"gdaemon": "1.0.0"},
        burned={"gdaemon": "1.1.0"},
        moved_aside=(aside.name,),
    )


def _admit_restart(*_args: object, **_kwargs: object) -> None:
    return None


def _stub_stop_admissions(
    monkeypatch: pytest.MonkeyPatch,
    *,
    refusal: str | None = None,
    waits: list[bool] | None = None,
) -> None:
    """Replace the stop admissions, which read the live daemon; record the wait flag."""

    def admissions(
        _ctx: click.Context, *, wait: bool, logged_stale_markers: object = None
    ) -> str | None:
        if waits is not None:
            waits.append(wait)
        return refusal

    monkeypatch.setattr(_cutover(), "_stop_admission_refusal", admissions)
    monkeypatch.setattr(_rollback(), "_stop_admission_refusal", admissions, raising=False)


@pytest.mark.usefixtures("bin_dir")
def test_restore_refuses_a_spawned_agent_before_anything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _RestoreSpy()
    monkeypatch.setattr(_rollback(), "run_restore", spy)
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-1")

    result = _run("restore", "99")

    assert result.exit_code != 0
    assert "spawned_agent_refused" in result.output
    assert spy.calls == []


@pytest.mark.usefixtures("bin_dir")
def test_restore_refuses_a_node_before_anything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_home = tmp_path / "node-home"
    node_home.mkdir()
    bootstrap = node_home / "bootstrap.yaml"
    bootstrap.write_text(NODE_BOOTSTRAP, encoding="utf-8")
    bootstrap.chmod(0o600)
    monkeypatch.setenv("GOBBY_HOME", str(node_home))
    spy = _RestoreSpy()
    monkeypatch.setattr(_rollback(), "run_restore", spy)

    result = _run("restore", "99")

    assert result.exit_code != 0
    assert NODE_REFUSAL in result.output
    assert spy.calls == []


def test_restore_defaults_to_restoring_only_gdaemon(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(
        bin_dir,
        ("gdaemon", b"build-one", "1.0.0"),
        ("ghook", b"hook-one", "2.0.0"),
    )
    spy = _RestoreSpy(outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"))
    monkeypatch.setattr(_rollback(), "run_restore", spy)
    _stub_stop_admissions(monkeypatch)

    result = _run("restore", generation.id)

    assert result.exit_code == 0, result.output
    assert len(spy.calls) == 1
    assert spy.calls[0]["generation"].id == generation.id
    assert spy.calls[0]["members"] == ["gdaemon"]


def test_restore_restores_the_members_named_with_bin(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(
        bin_dir,
        ("gcode", b"code-one", "1.0.0"),
        ("gdaemon", b"build-one", "1.0.0"),
        ("ghook", b"hook-one", "2.0.0"),
    )
    spy = _RestoreSpy(outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"))
    monkeypatch.setattr(_rollback(), "run_restore", spy)
    _stub_stop_admissions(monkeypatch)

    result = _run("restore", generation.id, "--bin", "gcode", "--bin", "ghook")

    assert result.exit_code == 0, result.output
    assert sorted(spy.calls[0]["members"]) == ["gcode", "ghook"]


def test_restore_all_restores_every_member_the_generation_records(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(
        bin_dir,
        ("gcode", b"code-one", "1.0.0"),
        ("gdaemon", b"build-one", "1.0.0"),
        ("ghook", b"hook-one", "2.0.0"),
    )
    spy = _RestoreSpy(outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"))
    monkeypatch.setattr(_rollback(), "run_restore", spy)
    _stub_stop_admissions(monkeypatch)

    result = _run("restore", generation.id, "--all")

    assert result.exit_code == 0, result.output
    assert sorted(spy.calls[0]["members"]) == ["gcode", "gdaemon", "ghook"]


def test_restore_presents_the_hold_token_to_run_restore(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    spy = _RestoreSpy(outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"))
    monkeypatch.setattr(_rollback(), "run_restore", spy)
    _stub_stop_admissions(monkeypatch)

    result = _run("restore", generation.id, "--hold-token", "hold-tok-5")

    assert result.exit_code == 0, result.output
    assert spy.calls[0]["hold_token"] == "hold-tok-5"


def test_restore_passes_no_hold_token_when_none_is_given(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    spy = _RestoreSpy(outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"))
    monkeypatch.setattr(_rollback(), "run_restore", spy)
    _stub_stop_admissions(monkeypatch)

    result = _run("restore", generation.id)

    assert result.exit_code == 0, result.output
    assert spy.calls[0]["hold_token"] is None


def test_restore_rejects_bin_together_with_all(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gcode", b"code-one", "1.0.0"))
    spy = _RestoreSpy()
    monkeypatch.setattr(_rollback(), "run_restore", spy)

    result = _run("restore", generation.id, "--bin", "gcode", "--all")

    assert result.exit_code == 2
    assert spy.calls == []


def test_restore_names_an_unknown_generation_and_exits_nonzero(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    spy = _RestoreSpy()
    monkeypatch.setattr(_rollback(), "run_restore", spy)

    result = _run("restore", "99")

    assert result.exit_code != 0
    assert "99" in result.output
    assert spy.calls == []


def test_restore_reports_a_restore_error_as_a_nonzero_exit(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    restore_error: type[Exception] = _restore().RestoreError
    spy = _RestoreSpy(error=restore_error("gdaemon 1.0.0 is burned; restore an older generation"))
    monkeypatch.setattr(_rollback(), "run_restore", spy)
    _stub_stop_admissions(monkeypatch)

    result = _run("restore", generation.id)

    assert result.exit_code != 0
    assert "gdaemon 1.0.0 is burned; restore an older generation" in result.output


def test_restore_prints_each_restored_member_with_its_version(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    spy = _RestoreSpy(outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"))
    monkeypatch.setattr(_rollback(), "run_restore", spy)
    _stub_stop_admissions(monkeypatch)

    result = _run("restore", generation.id)

    assert result.exit_code == 0, result.output
    assert "gdaemon" in result.output
    assert "1.0.0" in result.output


def test_restore_prints_the_burned_versions(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    spy = _RestoreSpy(outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"))
    monkeypatch.setattr(_rollback(), "run_restore", spy)
    _stub_stop_admissions(monkeypatch)

    result = _run("restore", generation.id)

    assert result.exit_code == 0, result.output
    assert "1.1.0" in result.output


def test_restore_prints_the_files_moved_aside(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    spy = _RestoreSpy(outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"))
    monkeypatch.setattr(_rollback(), "run_restore", spy)
    _stub_stop_admissions(monkeypatch)

    result = _run("restore", generation.id)

    assert result.exit_code == 0, result.output
    assert "gdaemon.previous" in result.output


def test_restore_start_refusal_checks_the_restart_against_the_generation_schema(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (bin_dir / ".gdaemon-schema-identity.json").write_text(
        json.dumps(SCHEMA_IDENTITY), encoding="utf-8"
    )
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    expected_seen: list[object] = []

    def restart_refusal(_runtime: object, _candidate: Path, *, expected_identity: object) -> None:
        expected_seen.append(expected_identity)
        return None

    monkeypatch.setattr(_rollback(), "restart_start_refusal", restart_refusal)
    _stub_stop_admissions(monkeypatch)
    spy = _RestoreSpy(
        outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"),
        probe=tmp_path / "candidate-gdaemon",
    )
    monkeypatch.setattr(_rollback(), "run_restore", spy)

    result = _run("restore", generation.id)

    assert result.exit_code == 0, result.output
    assert expected_seen == [SCHEMA_IDENTITY]
    assert spy.calls[0]["probed_start_refusal"] is None


def test_restore_start_refusal_returns_the_restart_refusal(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    refusal = "gdaemon identity differs from installed pin"
    monkeypatch.setattr(_rollback(), "restart_start_refusal", lambda *_a, **_k: refusal)
    _stub_stop_admissions(monkeypatch)
    spy = _RestoreSpy(
        outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"),
        probe=tmp_path / "candidate-gdaemon",
    )
    monkeypatch.setattr(_rollback(), "run_restore", spy)

    result = _run("restore", generation.id)

    assert result.exit_code == 0, result.output
    assert spy.calls[0]["probed_start_refusal"] == refusal


def test_restore_start_refusal_returns_a_stop_admission_refusal(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    refusal = "a restart-protected cron run is active; re-run with --wait"
    monkeypatch.setattr(_rollback(), "restart_start_refusal", _admit_restart)
    _stub_stop_admissions(monkeypatch, refusal=refusal)
    spy = _RestoreSpy(
        outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"),
        probe=tmp_path / "candidate-gdaemon",
    )
    monkeypatch.setattr(_rollback(), "run_restore", spy)

    result = _run("restore", generation.id)

    assert result.exit_code == 0, result.output
    assert spy.calls[0]["probed_start_refusal"] == refusal


def test_restore_stops_then_starts_the_daemon_with_restart_intent(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    events: list[str] = []

    def stop(
        _ctx: click.Context,
        docker_flag: bool,
        *,
        shutdown_intent: str,
        wait: bool,
        logged_stale_markers: object,
    ) -> bool:
        assert (docker_flag, shutdown_intent, wait) == (False, "restart", True)
        events.append("stop")
        return True

    def start(*, verbose: bool) -> None:
        assert verbose is False
        events.append("start")

    def fake_run_restore(
        _bin_dir: Path,
        _generation: object,
        _members: list[str],
        *,
        stop_daemon: Callable[[], bool],
        start_daemon: Callable[[], None],
        **_kwargs: object,
    ) -> object:
        assert stop_daemon()
        start_daemon()
        return _outcome(generation, tmp_path / "aside" / "gdaemon.previous")

    monkeypatch.setattr(_rollback(), "_do_stop", stop)
    monkeypatch.setattr(_rollback(), "start", start)
    monkeypatch.setattr(_rollback(), "run_restore", fake_run_restore)

    result = _run("restore", generation.id)

    assert result.exit_code == 0, result.output
    assert events == ["stop", "start"]


def test_restore_wait_reaches_the_stop_admissions(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _publish(bin_dir, ("gdaemon", b"build-one", "1.0.0"))
    waits: list[bool] = []
    monkeypatch.setattr(_rollback(), "restart_start_refusal", _admit_restart)
    _stub_stop_admissions(monkeypatch, waits=waits)
    spy = _RestoreSpy(
        outcome=_outcome(generation, tmp_path / "aside" / "gdaemon.previous"),
        probe=tmp_path / "candidate-gdaemon",
    )
    monkeypatch.setattr(_rollback(), "run_restore", spy)

    result = _run("restore", generation.id, "--wait")

    assert result.exit_code == 0, result.output
    assert waits == [True]
