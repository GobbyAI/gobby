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


def scan_line_is_malformed(line_num: int, raw_line: bytes, classify: ClassifyRecord) -> bool:
    """Decode one non-blank JSONL line, classify it, and report whether it is malformed."""
    try:
        value = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return True
    if not isinstance(value, dict):
        return True
    return classify(line_num, cast(dict[str, object], value)) is ScanVerdict.MALFORMED


def scan_jsonl(path: str | Path, classify: ClassifyRecord) -> ScanResult:
    """Decode JSONL records and delegate provider-specific shape validation."""
    last_malformed_line_num: int | None = None
    with Path(path).open("rb") as handle:
        for line_num, raw_line in enumerate(handle, start=1):
            if raw_line.strip() and scan_line_is_malformed(line_num, raw_line, classify):
                last_malformed_line_num = line_num
    return ScanResult(last_malformed_line_num=last_malformed_line_num)
