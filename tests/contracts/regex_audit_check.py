"""Regex audit: the classifier, the shape families, probes, consumer drivers and the two-engine run.

``classify`` gives each found pattern a verdict for its consumer class: exact as written, an exact
rewrite, a report row, or a failure. ``verify`` then sends every exact text through the Rust
driver in crates/gport/tests/regex_audit.rs and compares each probe with the Python observable of
the original pattern.

Signatures only: every function returns its empty value until the audit is implemented, and
``classify`` returns a ``fail`` sentinel.

``classify`` and ``verify`` call ``derive_probes``, ``work_entry`` and ``run_two_engine_driver``
through this module's namespace at call time, so a test can patch each of them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tests.contracts.regex_audit_tool import FoundRegex, Node

SUPPORTED_PYTHON_MINORS = ("3.13", "3.14")


@dataclass(frozen=True)
class Verdict:
    kind: Literal["as_written", "rewrite", "reported", "fail"]
    families: tuple[str, ...]
    python_text: str | None
    rust_text: str | None
    reason: str | None
    atoms: tuple[str, ...]
    decisions: tuple[str, ...]
    verified: bool
    universal: bool = False
    boundaries: tuple[Mapping[str, str], ...] = ()


@dataclass(frozen=True)
class DomainTable:
    python: Mapping[tuple[str, str], tuple[tuple[int, int], ...]] = field(default_factory=dict)
    rust: Mapping[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    python_boundary: Mapping[tuple[str, str, str], Mapping[str, tuple[int, ...]]] = field(
        default_factory=dict
    )
    rust_boundary: Mapping[str, Mapping[str, tuple[int, ...]]] = field(default_factory=dict)
    python_digest: Mapping[tuple[str, str, str], str] = field(default_factory=dict)

    def python_for(self, consumer: str, atom: str) -> tuple[tuple[int, int], ...]:
        return ()

    def version_stable(self, consumer: str, atom: Node) -> bool:
        return False

    def boundary_holds(self, consumer: str, atom: str, group: str) -> bool:
        return False


@dataclass(frozen=True)
class DriverResults:
    patterns: Mapping[int, object] = field(default_factory=dict)
    atoms: Mapping[int, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    boundaries: Mapping[int, Mapping[str, tuple[int, ...]]] = field(default_factory=dict)


STUB_VERDICT = Verdict(
    kind="fail",
    families=(),
    python_text=None,
    rust_text=None,
    reason="stub",
    atoms=(),
    decisions=(),
    verified=False,
)


def by_version(value: object, minor: str | None = None) -> object:
    return None


def construct_rows() -> tuple[Mapping[str, object], ...]:
    return ()


def python_invalid_minors(pattern: str, rows: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    return ()


def consumer_flags(consumer: str) -> int:
    return 0


def work_entry(
    index: int, rust_text: str, consumer: str, probes: Sequence[str]
) -> dict[str, object]:
    return {}


def compare_observable(
    consumer: str, python_observable: object, driver_result: object
) -> str | None:
    return None


def group_spans(match: re.Match[str]) -> tuple[tuple[int, int] | None, ...]:
    return ()


def python_membership(atom: str, engine: Literal["re", "regex"]) -> tuple[tuple[int, int], ...]:
    return ()


def python_boundary(atom: str, engine: Literal["re", "regex"], subject: str) -> tuple[int, ...]:
    return ()


def membership_digest(ranges: Sequence[tuple[int, int]]) -> str:
    return ""


def run_two_engine_driver(
    patterns: Sequence[Mapping[str, object]],
    atoms: Sequence[Mapping[str, object]],
    boundaries: Sequence[Mapping[str, object]] = (),
) -> DriverResults:
    return DriverResults()


def domain_table(
    atoms: Iterable[tuple[str, str]],
    boundaries: Iterable[tuple[str, str]] = (),
    boundary_rows: Sequence[Mapping[str, object]] = (),
    membership_rows: Mapping[str, object] | None = None,
) -> DomainTable:
    return DomainTable()


def classify(pattern: str, consumer: str, domain: DomainTable) -> Verdict:
    return STUB_VERDICT


def verify(rows: Sequence[tuple[FoundRegex, Verdict]]) -> list[tuple[FoundRegex, Verdict]]:
    return []


def matches_everything(python_text: str, consumer: str) -> bool:
    return False


def literals_independent(x: str, y: str, flags: int, domain: DomainTable) -> bool | None:
    return False


def rust_safe(text: str) -> list[str]:
    return []


def to_rust_text(python_text: str, consumer: str, domain: DomainTable) -> str:
    return ""


def observable(consumer: str, pattern: str, text: str, *, without: str | None = None) -> object:
    return None


def derive_probes(original: str, python_text: str, consumer: str, limit: int = 2000) -> list[str]:
    return []


def check_rewrite(
    original: str, python_text: str, consumer: str, inputs: Iterable[str]
) -> str | None:
    return None


def exhaustive_inputs(alphabet: str, max_len: int) -> Iterator[str]:
    return iter(())


def drive_consumer(
    case: Mapping[str, object],
    text: str,
    *,
    pattern: str | None = None,
    tmp: Path | None = None,
) -> object:
    return None


def record_consumer_cases(path: Path) -> None:
    return None


def record_regex_audit(out_dir: Path, questions_path: Path, consumers_path: Path) -> None:
    return None
