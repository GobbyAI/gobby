import json
from pathlib import Path

import pytest

from gobby.agents.watchdog._scan import ScanVerdict, scan_jsonl

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
