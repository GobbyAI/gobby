import json
import re
from pathlib import Path

import pytest

from gobby.agents.watchdog import _scan
from gobby.agents.watchdog._scan import ScanVerdict, find_turn_start, scan_jsonl

pytestmark = pytest.mark.unit


def test_scan_jsonl_ignores_blank_lines_and_accepts_shape_neutral_dicts(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n  \n" + json.dumps({"params": {"update": {"sessionUpdate": "turn_completed"}}}),
        encoding="utf-8",
    )
    seen: list[tuple[int, dict[str, object]]] = []

    def classify(line_num: int, value: dict[str, object]) -> ScanVerdict:
        seen.append((line_num, value))
        return ScanVerdict.VALID

    result = scan_jsonl(path, classify)

    assert [line_num for line_num, _ in seen] == [3]
    assert result.last_malformed_line_num is None


def test_scan_jsonl_tracks_decode_json_nondict_and_classifier_failures(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(b'\xff\n{broken\n[]\n{"recognized": true}\n')

    result = scan_jsonl(path, lambda _line, _value: ScanVerdict.MALFORMED)

    assert result.last_malformed_line_num == 4


def test_scan_jsonl_does_not_poison_ignored_records(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text('{"type":"future_record"}\n', encoding="utf-8")

    result = scan_jsonl(path, lambda _line, _value: ScanVerdict.IGNORED)

    assert result.last_malformed_line_num is None


_START_MARKER = re.compile(rb'"kind"\s*:\s*"start"')


def _starts(value: dict[str, object]) -> bool:
    return value.get("kind") == "start"


@pytest.mark.parametrize("chunk_bytes", [7, 64, 1 << 20])
def test_find_turn_start_returns_the_latest_confirmed_complete_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunk_bytes: int
) -> None:
    monkeypatch.setattr(_scan, "_TURN_SEARCH_CHUNK_BYTES", chunk_bytes)
    lines = [
        b'{"kind": "start", "n": 1}\n',
        b'{"kind": "other"}\n',
        b'{"pad": "' + b"x" * 200 + b'", "kind":"start"}\n',
        b'{"kind": "other", "inner": {"kind": "start"}}\n',
        b'{broken "kind": "start"\n',
        b'{"kind": "start", "n": 6}',
    ]
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(b"".join(lines))

    with path.open("rb") as handle:
        found = find_turn_start(handle, path.stat().st_size, _START_MARKER, _starts)

    assert found == (len(lines[0]) + len(lines[1]), 2)


def test_find_turn_start_without_a_confirmed_start_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(b'{"kind": "other", "inner": {"kind": "start"}}\n{broken "kind": "start"\n')

    with path.open("rb") as handle:
        found = find_turn_start(handle, path.stat().st_size, _START_MARKER, _starts)

    assert found is None
