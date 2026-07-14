"""Tests for the recall-signals backfill CLI (#18196 rotation awareness)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.memory.signals import recall_signals

pytestmark = pytest.mark.unit


def _invoke_backfill(args: list[str]) -> tuple[MagicMock, object]:
    store = MagicMock()
    store.load_signal_events_jsonl.return_value = 2
    with (
        patch("gobby.cli.memory.signals.open_runtime_hub_database") as mock_open_db,
        patch("gobby.cli.memory.signals.RecallSignalStore", return_value=store),
    ):
        mock_open_db.return_value = MagicMock()
        result = CliRunner().invoke(recall_signals, ["backfill-events", *args])
    return store, result


def test_backfill_default_loads_rotated_files_oldest_first(tmp_path: Path) -> None:
    live = tmp_path / "recall_signal.jsonl"
    backup_one = tmp_path / "recall_signal.jsonl.1"
    backup_two = tmp_path / "recall_signal.jsonl.2"
    for file in (live, backup_one, backup_two):
        file.write_text("{}\n", encoding="utf-8")

    with patch("gobby.cli.memory.signals.resolve_recall_signal_path", return_value=live):
        store, result = _invoke_backfill([])

    assert result.exit_code == 0, result.output
    loaded = [call.args[0] for call in store.load_signal_events_jsonl.call_args_list]
    assert loaded == [backup_two, backup_one, live]


def test_backfill_explicit_path_loads_only_that_file(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.jsonl"
    explicit.write_text("{}\n", encoding="utf-8")

    store, result = _invoke_backfill(["--path", str(explicit)])

    assert result.exit_code == 0, result.output
    loaded = [call.args[0] for call in store.load_signal_events_jsonl.call_args_list]
    assert loaded == [explicit]


def test_backfill_errors_when_no_log_exists(tmp_path: Path) -> None:
    with patch(
        "gobby.cli.memory.signals.resolve_recall_signal_path",
        return_value=tmp_path / "recall_signal.jsonl",
    ):
        store, result = _invoke_backfill([])

    assert result.exit_code != 0
    assert "No recall-signal log" in result.output
    store.load_signal_events_jsonl.assert_not_called()
