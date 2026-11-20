"""Regex audit: enumeration of the definition universe, the lexer and the report writer.

The universe is every regex the production enumerator finds in the bundled definition
directories, plus the production contrastive constants. Each found pattern is attributed to the
consumer class whose production call runs it, and the class decides the Python engine, the
external flags and the driver mode the audit uses for it.

Signatures only: every function returns its empty value until the audit is implemented.

Contracts the tests rely on: a class Node's ``detail`` holds ``items`` (its member atom texts)
and ``negated``; ``ACCEPTED_DIVERGENCES`` is read through this module at call time, so a test
can patch it; the JSON report holds ``rows`` and ``decisions`` (each with ``id``, ``python``
keyed by minor, and ``rust``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tests.contracts.regex_audit_check import DomainTable, Verdict

REPO = Path(__file__).resolve().parents[2]
UNIVERSE_ROOTS: tuple[tuple[Path, tuple[str, ...]], ...] = (
    (REPO / "src" / "gobby" / "install" / "shared", ("*.yaml", "*.toml")),
    (REPO / ".gobby" / "workflows" / "rules", ("*.yaml",)),
)
PYTHON_CONSTANT_SOURCE = (
    "python:gobby.workflows.assistant_response_matching.ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS"
)

CONSUMER_CLASSES: tuple[str, ...] = (
    "BOOL",
    "INERT-EXEMPTION",
    "FULLMATCH",
    "MATCH-GROUPS-FIRSTLINE",
    "SEARCH-PKG-M",
    "SPAN-ALLOWLIST",
    "FULLMATCH-PATH",
    "SEARCH-GROUP",
    "SUB",
    "UNKNOWN",
)

# How a found pattern reaches its class: a key the production enumerator yields, a condition call
# or a template filter that holds the literal, or the production constant. A key not listed here
# is UNKNOWN; a command_not_pattern with no truthy sibling command_pattern is INERT-EXEMPTION.
CONSUMERS: Mapping[str, str] = {
    "command_pattern": "BOOL",
    "command_not_pattern": "BOOL",
    "value_pattern": "FULLMATCH",
    "consensus_pattern": "MATCH-GROUPS-FIRSTLINE",
    "line_regex": "SEARCH-PKG-M",
    "condition:assistant_response_matches_any": "SPAN-ALLOWLIST",
    "condition:write_paths_match": "FULLMATCH-PATH",
    "filter:regex_search": "SEARCH-GROUP",
    "filter:regex_replace": "SUB",
    "constant": "SPAN-ALLOWLIST",
}

SUBJECT_DOMAINS: Mapping[str, str] = {
    consumer: ("scalar-first-line" if consumer == "MATCH-GROUPS-FIRSTLINE" else "scalar")
    for consumer in CONSUMER_CLASSES
}
CONSUMER_ENGINES: Mapping[str, Literal["re", "regex"]] = {
    consumer: ("regex" if consumer == "SEARCH-PKG-M" else "re") for consumer in CONSUMER_CLASSES
}
CONSUMER_FLAGS: Mapping[str, frozenset[str]] = dict.fromkeys(CONSUMER_CLASSES, frozenset()) | {
    "SPAN-ALLOWLIST": frozenset({"case_insensitive"}),
    "SEARCH-PKG-M": frozenset({"multi_line"}),
}
Mode = Literal["search", "match", "fullmatch", "sub"]
_DEFAULT_MODE: Mode = "search"
CONSUMER_MODES: Mapping[str, Mode] = dict.fromkeys(CONSUMER_CLASSES, _DEFAULT_MODE) | {
    "FULLMATCH": "fullmatch",
    "FULLMATCH-PATH": "fullmatch",
    "MATCH-GROUPS-FIRSTLINE": "match",
    "SUB": "sub",
}
BOUNDARY_SUBJECTS: Mapping[str, tuple[str, ...]] = {
    "empty": ("",),
    "nonword_only": ("!", "!!", " "),
    "word_edge": ("a", "a!", "!a", "ab"),
}
MEMBERSHIP_BASIS: Mapping[str, object] = {
    "shorthand": ("\\s", "\\S", "\\d", "\\D", "\\w", "\\W"),
    "ignore_case_literals": (0x00, 0x7F),
}
SUB_REPLACEMENT = "n"
DOMAIN_TRANSLATION_MAX_RANGES = 64
ACCEPTED_DIVERGENCES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FoundRegex:
    source: str
    location: str
    pattern: str
    consumer: str
    via: str
    reference: bool = False


@dataclass(frozen=True)
class Node:
    kind: str
    span: tuple[int, int]
    detail: Mapping[str, object]
    children: tuple[Node, ...]


class UnknownConstruct(ValueError):
    """A construct the lexer does not know; the message is the construct's text."""


def load_documents(path: Path) -> list[object]:
    return []


def enumerate_regexes(data: object, source: str) -> list[FoundRegex]:
    return []


def scan_universe(
    roots: Sequence[tuple[Path, tuple[str, ...]]] = UNIVERSE_ROOTS,
    repo: Path = REPO,
    include_constants: bool = True,
) -> list[FoundRegex]:
    return []


def lex(pattern: str) -> Node:
    return Node("stub", (0, 0), {}, ())


def sre_shape(pattern: str, flags: int) -> dict[str, int]:
    return {}


def lex_shape(tree: Node) -> dict[str, int]:
    return {}


def engine_atoms(tree: Node, consumer: str) -> list[tuple[str, str]]:
    return []


def boundary_atoms(tree: Node, consumer: str) -> list[tuple[str, str, Node]]:
    return []


def top_level_alternatives(tree: Node) -> tuple[Node, ...]:
    return ()


def min_width(node: Node) -> int:
    return 0


def single_match_only(tree: Node, consumer: str) -> bool:
    return False


def flag_inert(tree: Node, consumer: str, flag: str) -> bool:
    return False


def class_matches_newline(node: Node) -> bool:
    return False


def basis_members(atom: Node, consumer: str) -> tuple[str, ...] | None:
    return None


def write_report(
    rows: Sequence[tuple[FoundRegex, Verdict]],
    json_path: Path,
    md_path: Path,
    questions_path: Path | None,
    *,
    domain: DomainTable | None = None,
) -> None:
    """Write the JSON report, the Markdown report and the operator questions file.

    ``domain`` supplies the measured sets and offsets the semantic decision rows list.
    """
    return None
