"""Shared JSONL scanner for provider-specific watchdog readers."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast


class ScanVerdict(StrEnum):
    VALID = "valid"
    IGNORED = "ignored"
    MALFORMED = "malformed"


ClassifyRecord = Callable[[int, dict[str, object]], ScanVerdict]


@dataclass(frozen=True, slots=True)
class ScanResult:
    last_malformed_line_num: int | None = None


def scan_jsonl(path: str | Path, classify: ClassifyRecord) -> ScanResult:
    """Decode JSONL records and delegate provider-specific shape validation."""
    last_malformed_line_num: int | None = None
    with Path(path).open("rb") as handle:
        for line_num, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                decoded = raw_line.decode("utf-8")
                value = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError):
                last_malformed_line_num = line_num
                continue
            if not isinstance(value, dict):
                last_malformed_line_num = line_num
                continue
            verdict = classify(line_num, cast(dict[str, object], value))
            if verdict is ScanVerdict.MALFORMED:
                last_malformed_line_num = line_num
    return ScanResult(last_malformed_line_num=last_malformed_line_num)
