"""Generic regex portability audit.

Every regex the production enumerator finds in the bundled definition directories, plus the
production contrastive constants, gets a verdict for the consumer class whose production call
runs it: exact as written, an exact rewrite, a report row, or a failure. A verdict is exact only
after the Rust driver in crates/gport/tests/regex_audit.rs agrees with the Python observable of
the original pattern on every probe. Every case here is synthetic: the fixtures under
tests/contracts/regex_audit/ and the patterns written in the tests use short invented literals.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any, Literal
from unittest import mock

import pytest
import regex
import yaml

from gobby.agents.detection.safe_regex import compile_safe_regex
from gobby.workflows.assistant_response_matching import (
    ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS,
    first_response_match,
)
from gobby.workflows.regex_validation import iter_definition_regexes
from gobby.workflows.templates import TemplateEngine
from tests.contracts import regex_audit_check as check
from tests.contracts import regex_audit_classify as classify
from tests.contracts import regex_audit_tool as tool
from tests.contracts import rules_corpus

CONTRACTS_DIR = Path(__file__).resolve().parent
REPO = CONTRACTS_DIR.parents[1]
FIXTURE_DIR = CONTRACTS_DIR / "regex_audit"
CONSTRUCTS_FIXTURE = FIXTURE_DIR / "constructs.json"
SHAPES_FIXTURE = FIXTURE_DIR / "shape_families.json"
CONSUMERS_FIXTURE = FIXTURE_DIR / "consumers.json"
README = FIXTURE_DIR / "README.md"
RECORD_ENV = "GOBBY_CORPUS_RECORD"
QUESTIONS_ENV = "GOBBY_REGEX_QUESTIONS_PATH"
RUNNING_MINOR = f"{sys.version_info[0]}.{sys.version_info[1]}"
RangeSet = tuple[tuple[int, int], ...]
DOMAIN_MEMBER_KEYS: tuple[tuple[Literal["re", "regex"], str], ...] = (
    ("re", "python_members"),
    ("regex", "regex_members"),
)
ALL_SCALARS: RangeSet = ((0x0, 0xD7FF), (0xE000, 0x10FFFF))
NO_SCALARS: RangeSet = ()
EMPTY = check.DomainTable()
VERDICT_KINDS = ("as_written", "rewrite", "reported", "fail")
FLAGS = ("case_insensitive", "multi_line")
BOUNDARY_ATOM_TEXTS = ("\\b", "\\B", "(?i:\\b)", "(?i:\\B)")
WITHOUT_CONSUMER_CALL = ("INERT-EXEMPTION", "UNKNOWN")
FAMILY_IDS = ("R0", "R1", "R2+", "R2-", "R3", "R4", "R5", "D1", "T-END", "T-ESC", "T-CLASS")
CONSUMING_FAMILIES = ("R0", "R1", "R2+", "R2-", "R3", "R4", "R5", "D1")
STRUCTURAL_CASE_REASONS = (
    "lookaround-under-repetition",
    "lookaround-overlapping-literals",
    "lookaround-fold-not-version-stable",
    "lookaround-newline-body",
    "sub-empty-match-iteration",
)
# Reasons of the first three steps of the order of reasons: inert, shape, empty-match iteration.
EARLY_REASONS = (
    "inert-no-command-pattern",
    "inert-not-allowlisted",
    "dollar-before-final-newline",
    "search-package-syntax",
    "sub-empty-match-iteration",
)
REPORT_REASONS = frozenset(
    {
        "lookaround-non-boolean",
        "lookaround-continuation",
        "lookaround-newline-body",
        "lookaround-with-groups",
        "lookaround-unsupported-shape",
        "dollar-before-final-newline",
        "search-package-syntax",
        "inert-not-allowlisted",
        "inert-no-command-pattern",
        "lookaround-under-repetition",
        "lookaround-overlapping-literals",
        "lookaround-fold-not-version-stable",
        "no-distinguishing-witness",
        "sub-empty-match-iteration",
        "no-multi-match-witness",
        "no-flag-witness",
        "unverified-proposal",
    }
)
SEMANTIC_PREFIX = "semantic-decision:"
REQUIRED_CONSTRUCT_IDS = (
    "literal",
    "non-ascii-literal",
    "any",
    "class",
    "negated-class",
    "class-range",
    "start-caret",
    "capture-group",
    "non-capture-group",
    "alternation",
    "star",
    "plus",
    "question",
    "counted-repeat",
    "lazy-repeat",
    "space-shorthand",
    "not-space-shorthand",
    "digit-shorthand",
    "not-digit-shorthand",
    "word-shorthand",
    "not-word-shorthand",
    "word-boundary",
    "non-word-boundary",
    "inline-flag-i",
    "lookahead",
    "negative-lookahead",
    "lookbehind",
    "negative-lookbehind",
    "backreference",
    "named-backreference",
    "atomic",
    "possessive",
    "conditional",
    "end-of-string-Z",
    "end-of-string-z",
    "start-A",
    "escaped-angle",
    "escaped-punctuation",
    "bracket-in-class",
    "class-intersection",
    "class-difference",
    "class-symmetric",
    "literal-brace",
    "brace-comma-n",
    "named-group-P",
    "named-group-angle",
    "inline-flag-m",
    "inline-flag-s",
    "scoped-flag",
    "verbose-flag",
    "unicode-property",
    "posix-class",
    "lazy",
    "dollar",
    "multiline-dollar",
    "octal-escape",
    "hex-escape",
    "unicode-escape",
    "named-char",
    "class-backspace",
    "empty-alternative",
)
REQUIRED_SHAPES: Mapping[str, Mapping[str, int]] = {
    "lookahead": {"groups": 0, "lookahead": 1},
    "negative-lookahead": {"groups": 0, "negative_lookahead": 1},
    "lookbehind": {"groups": 0, "lookbehind": 1},
    "backreference": {"groups": 1, "backref": 1},
    "capture-group": {"groups": 1},
    "named-group-P": {"groups": 1},
    "non-capture-group": {"groups": 0},
}
DISPOSITIONS = frozenset(
    {"portable", "engine-dependent", "translate", "lookaround", "consumer-dependent", "fail"}
)

# The module attribute through which each consumer driver's production call reaches its engine.
FLAG_GUARD_PATCH_SITES: Mapping[str, tuple[str, str]] = {
    "command_patterns_match": ("gobby.workflows.engine.command_matching", "re"),
    "attestation_shape_refusal": ("gobby.tasks.attestation_shapes", "re"),
    "advance_seat": ("gobby.workflows.engine.seat_transitions", "re"),
    "write_paths_match": ("gobby.workflows.condition_helpers_paths", "re"),
    "regex_search_filter": ("gobby.workflows.templates", "re"),
    "regex_replace_filter": ("gobby.workflows.templates", "re"),
    "compile_safe_regex": ("gobby.agents.detection.safe_regex", "regex"),
}
# (class, flag) pairs that need no flag-guard case, with the reason the flag cannot change a result.
FLAG_GUARD_EXEMPTIONS: Mapping[tuple[str, str], str] = {
    ("MATCH-GROUPS-FIRSTLINE", "multi_line"): "single-line-subject",
    ("SPAN-ALLOWLIST", "multi_line"): "no-anchor-in-allowlist",
}
FLAG_ARGUMENT_INDEX = {"search": 2, "match": 2, "fullmatch": 2, "sub": 4, "compile": 1}


# ---------------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"{path.name} is missing"
    document = json.loads(path.read_text())
    assert isinstance(document, dict), f"{path.name} is not a JSON object"
    return document


def _version(value: object) -> object:
    """A fixture value read for the running minor; a version-keyed value must hold that key."""
    if isinstance(value, dict):
        assert RUNNING_MINOR in value, f"no value for Python {RUNNING_MINOR} in {value}"
    return check.by_version(value)


def _has_version(value: object) -> bool:
    return not isinstance(value, dict) or RUNNING_MINOR in value


def _version_keys_complete(value: object) -> bool:
    return not isinstance(value, dict) or set(value) == set(check.SUPPORTED_PYTHON_MINORS)


def _verdict(pattern: str, consumer: str, table: check.DomainTable = EMPTY) -> check.Verdict:
    verdict = check.classify(pattern, consumer, table)
    assert isinstance(verdict, check.Verdict), f"classify({pattern!r}) returned {verdict!r}"
    return verdict


def _expect(
    pattern: str,
    consumer: str,
    kind: str,
    reason: str | None = None,
    table: check.DomainTable = EMPTY,
) -> check.Verdict:
    verdict = _verdict(pattern, consumer, table)
    assert verdict.kind == kind, f"{pattern!r} under {consumer}: {verdict}"
    if reason is not None:
        assert verdict.reason == reason, f"{pattern!r} under {consumer}: {verdict}"
    return verdict


def _fail_python_invalid() -> check.Verdict:
    return check.Verdict(
        kind="fail",
        families=(),
        python_text=None,
        rust_text=None,
        reason="python-invalid",
        atoms=(),
        decisions=(),
        verified=False,
    )


def _engine_consumer(engine: str) -> str:
    return "SEARCH-PKG-M" if engine == "regex" else "BOOL"


def _stable_table(
    python: Mapping[tuple[str, str], Sequence[tuple[int, int]]],
    rust: Mapping[str, Sequence[tuple[int, int]]] | None = None,
    *,
    python_boundary: Mapping[tuple[str, str, str], Mapping[str, tuple[int, ...]]] | None = None,
    rust_boundary: Mapping[str, Mapping[str, tuple[int, ...]]] | None = None,
) -> check.DomainTable:
    """A table in which every atom of ``python`` is version-stable.

    ``rust`` defaults to the same set per atom text. Each basis atom an atom depends on gets one
    digest, equal on every supported minor: the digest of the basis atom's own entry when the
    table holds one, else of the atom's entry.
    """
    entries: dict[tuple[str, str], RangeSet] = {
        key: tuple((low, high) for low, high in value) for key, value in python.items()
    }
    rust_entries: dict[str, RangeSet] = (
        {atom: tuple((low, high) for low, high in value) for atom, value in rust.items()}
        if rust is not None
        else {atom: value for (_, atom), value in entries.items()}
    )
    digests: dict[tuple[str, str, str], str] = {}
    for (engine, atom), value in entries.items():
        members = tool.basis_members(tool.lex(atom), _engine_consumer(engine))
        for basis in members or ():
            source = entries.get((engine, basis), value)
            for minor in check.SUPPORTED_PYTHON_MINORS:
                digests[(engine, minor, basis)] = check.membership_digest(source)
    return check.DomainTable(
        python=entries,
        rust=rust_entries,
        python_boundary=dict(python_boundary or {}),
        rust_boundary=dict(rust_boundary or {}),
        python_digest=digests,
    )


def _replace(table: check.DomainTable, **changes: Any) -> check.DomainTable:
    return dataclasses.replace(table, **changes)


def _split_digest(
    table: check.DomainTable, engine: str, basis: str, minor: str = "3.14"
) -> check.DomainTable:
    """The table with one minor's digest of ``basis`` changed, so that atom is not stable."""
    digests = dict(table.python_digest)
    digests[(engine, minor, basis)] = "synthetic-other-digest"
    return _replace(table, python_digest=digests)


def _hex_literal(character: str) -> str:
    return f"(?i:\\x{ord(character):02x})"


def _ranges(points: Sequence[int]) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for point in sorted(set(points)):
        if ranges and ranges[-1][1] + 1 == point:
            ranges[-1] = (ranges[-1][0], point)
        else:
            ranges.append((point, point))
    return tuple(ranges)


def _points(ranges: Sequence[Sequence[int]]) -> set[int]:
    return {point for low, high in ranges for point in range(low, high + 1)}


def _cut(ranges: Sequence[Sequence[int]], low: int, high: int) -> list[list[int]]:
    return [
        [max(start, low), min(end, high)] for start, end in ranges if end >= low and start <= high
    ]


def _complement(ranges: Sequence[Sequence[int]]) -> tuple[tuple[int, int], ...]:
    members = _points(ranges)
    outside = [
        point for low, high in ALL_SCALARS for point in range(low, high + 1) if point not in members
    ]
    return _ranges(outside)


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _independent_universe() -> list[tuple[str, str, str]]:
    """Every (source, location, pattern) the production enumerator finds, found independently."""
    rows: list[tuple[str, str, str]] = []
    for root, globs in tool.UNIVERSE_ROOTS:
        for glob in globs:
            for path in sorted(root.rglob(glob)):
                text = path.read_text()
                if path.suffix == ".toml":
                    documents: list[object] = [tomllib.loads(text)]
                else:
                    documents = list(yaml.safe_load_all(text))
                source = path.relative_to(REPO).as_posix()
                for document in documents:
                    for found in iter_definition_regexes(document):
                        rows.append((source, found.field, found.pattern))
    for index, pattern in enumerate(ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS):
        rows.append((tool.PYTHON_CONSTANT_SOURCE, f"[{index}]", pattern))
    return rows


def _lexed(pattern: str) -> tool.Node | None:
    try:
        return tool.lex(pattern)
    except tool.UnknownConstruct:
        return None


def _fixture_digest(membership: Mapping[str, Any], engine: str, basis: str, minor: str) -> Any:
    """The fixture's digest of a basis atom on one minor, or None when no row holds it."""
    for row in membership.get("shorthand", []):
        if row["engine"] == engine and row["atom"] == basis:
            value = row["digest"]
            if isinstance(value, dict):
                return value.get(minor)
            return value
    literal = re.fullmatch(r"\(\?i:\\x([0-9a-f]{2})\)", basis)
    if literal is not None:
        for row in membership.get("ignore_case_ascii", []):
            if row["engine"] == engine:
                value = row["block_sha256"]
                return value.get(minor) if isinstance(value, dict) else value
    return None


def _recomputed_stable(
    consumer: str, atom: str, membership: Mapping[str, Any], domain: check.DomainTable
) -> bool:
    """Version stability recomputed from basis_members, the fixture rows and a composition."""
    node = tool.lex(atom)
    members = tool.basis_members(node, consumer)
    if members is None:
        return False
    engine = tool.CONSUMER_ENGINES[consumer]
    for basis in members:
        values = [
            _fixture_digest(membership, engine, basis, minor)
            for minor in check.SUPPORTED_PYTHON_MINORS
        ]
        if any(value is None for value in values) or len({json.dumps(v) for v in values}) != 1:
            return False
    items = node.detail.get("items") if node.kind == "class" else None
    if isinstance(items, Sequence) and not isinstance(items, str):
        composed: set[int] = set()
        for item in items:
            composed |= _points(check.python_membership(str(item), engine))
        if node.detail.get("negated"):
            composed = _points(_complement(_ranges(sorted(composed))))
        if composed != _points(domain.python_for(consumer, atom)):
            return False
    return True


@cache
def _live() -> tuple[list[tool.FoundRegex], check.DomainTable, dict[str, Any]]:
    """The universe and the live table measured for it (one driver call for the session)."""
    found = tool.scan_universe()
    constructs = json.loads(CONSTRUCTS_FIXTURE.read_text())
    atoms: set[tuple[str, str]] = set()
    boundaries: set[tuple[str, str]] = set()
    for item in found:
        tree = _lexed(item.pattern)
        if tree is None:
            continue
        engine = tool.CONSUMER_ENGINES[item.consumer]
        atoms |= {(engine, atom) for _, atom in tool.engine_atoms(tree, item.consumer)}
        boundaries |= {(engine, atom) for _, atom, _ in tool.boundary_atoms(tree, item.consumer)}
    for row in constructs["constructs"]:
        if row["disposition"] != "engine-dependent":
            continue
        tree = _lexed(row["exemplar"])
        for consumer in ("BOOL", "SEARCH-PKG-M"):
            if tree is not None:
                engine = tool.CONSUMER_ENGINES[consumer]
                atoms |= {(engine, atom) for _, atom in tool.engine_atoms(tree, consumer)}
                boundaries |= {(engine, atom) for _, atom, _ in tool.boundary_atoms(tree, consumer)}
    domain = check.domain_table(
        sorted(atoms), sorted(boundaries), constructs["boundary"], constructs["membership"]
    )
    return found, domain, constructs


# ---------------------------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------------------------

SYNTHETIC_TREE: Mapping[str, Any] = {
    "rules": [
        {
            "name": "synthetic-rx-one",
            "effects": [
                {"type": "block", "command_pattern": "syn-[0-9]+", "command_not_pattern": "--dry"},
                {"type": "block", "command_not_pattern": "synq"},
            ],
            "when": (
                "write_paths_match(event.data, tool_input, './syn/.*') and "
                "write_paths_match(event.data, tool_input, pattern='./syn/b.*')"
            ),
        },
        {"attestation_shape": {"reference": {"value_pattern": "syn-[a-z]{3}"}}},
        {"protocol": {"consensus_pattern": "^SYN (agree|object)$"}},
        {"detection": {"line_regex": ["^syn>", "go$"]}},
        {"pattern": "q+"},
        {"synthetic_regex": "x*"},
        {"when": "assistant_response_matches_any(patterns=['syn'], regex=true)"},
        {"when": "assistant_response_matches_any([\n    'synq',\n    'opt'], True)"},
        {"when": "assistant_response_matches_any(['dry'], False)"},
        {
            "message": (
                "{{ t | regex_search('syn-[0-9]+') }} "
                "{{ t | regex_replace(pattern='syn-[a-z]+', replacement='n') }}"
            )
        },
    ]
}
SYNTHETIC_TREE_EXPECTED = [
    ("rules[0].effects[0].command_pattern", "BOOL", "syn-[0-9]+"),
    ("rules[0].effects[0].command_not_pattern", "BOOL", "--dry"),
    ("rules[0].effects[1].command_not_pattern", "INERT-EXEMPTION", "synq"),
    ("rules[0].when", "FULLMATCH-PATH", "./syn/.*"),
    ("rules[0].when", "FULLMATCH-PATH", "./syn/b.*"),
    ("rules[1].attestation_shape.reference.value_pattern", "FULLMATCH", "syn-[a-z]{3}"),
    ("rules[2].protocol.consensus_pattern", "MATCH-GROUPS-FIRSTLINE", "^SYN (agree|object)$"),
    ("rules[3].detection.line_regex[0]", "SEARCH-PKG-M", "^syn>"),
    ("rules[3].detection.line_regex[1]", "SEARCH-PKG-M", "go$"),
    ("rules[4].pattern", "UNKNOWN", "q+"),
    ("rules[5].synthetic_regex", "UNKNOWN", "x*"),
    ("rules[6].when", "SPAN-ALLOWLIST", "syn"),
    ("rules[7].when", "SPAN-ALLOWLIST", "synq"),
    ("rules[7].when", "SPAN-ALLOWLIST", "opt"),
    ("rules[9].message", "SEARCH-GROUP", "syn-[0-9]+"),
    ("rules[9].message", "SUB", "syn-[a-z]+"),
]


def test_enumerator_attribution_matches_production_enumerator() -> None:
    found = tool.enumerate_regexes(SYNTHETIC_TREE, "synthetic.yaml")
    observed = [(item.location, item.consumer, item.pattern) for item in found]
    assert observed == SYNTHETIC_TREE_EXPECTED, f"attribution: {observed}"
    production = Counter((r.field, r.pattern) for r in iter_definition_regexes(SYNTHETIC_TREE))
    assert Counter((item.location, item.pattern) for item in found) == production
    assert all(item.source == "synthetic.yaml" for item in found)
    vias = {item.location: item.via for item in found}
    assert vias["rules[0].effects[0].command_pattern"] == "key"
    assert vias["rules[0].when"] == "condition:write_paths_match"
    assert vias["rules[6].when"] == "condition:assistant_response_matches_any"
    assert {item.via for item in found if item.location == "rules[9].message"} == {
        "filter:regex_search",
        "filter:regex_replace",
    }


# ---------------------------------------------------------------------------------------------
# Lexer against Python's parser
# ---------------------------------------------------------------------------------------------


def _parser_width(pattern: str, flags: int) -> int:
    parser = importlib.import_module("re._parser")
    width = parser.parse(pattern, flags).getwidth()[0]
    return int(width)


def _parses(pattern: str, flags: int) -> bool:
    try:
        importlib.import_module("re._parser").parse(pattern, flags)
    except re.error:
        return False
    return True


def test_lexer_agrees_with_python_parser() -> None:
    document = _load(CONSTRUCTS_FIXTURE)
    exemplars: list[tuple[str, int]] = []
    for row in document["constructs"]:
        compiles = row["python_compiles"]
        if not _has_version(compiles):
            continue
        if not _version(compiles):
            continue
        exemplars.append((row["exemplar"], 0))
        if row["id"] in REQUIRED_SHAPES:
            assert row.get("shape") == REQUIRED_SHAPES[row["id"]], f"{row['id']}: shape"
        if "shape" in row:
            assert tool.sre_shape(row["exemplar"], 0) == row["shape"], f"{row['id']}: sre"
            assert tool.lex_shape(tool.lex(row["exemplar"])) == row["shape"], f"{row['id']}: lex"
    present = {row["id"] for row in document["constructs"]}
    assert set(REQUIRED_SHAPES) <= present, "a required shape anchor is missing"
    for item in tool.scan_universe():
        flags = check.consumer_flags(item.consumer) if item.consumer != "SEARCH-PKG-M" else 0
        exemplars.append((item.pattern, flags))
    for pattern, flags in exemplars:
        if not _parses(pattern, flags):
            continue
        tree = tool.lex(pattern)
        assert tool.lex_shape(tree) == tool.sre_shape(pattern, flags), pattern
        assert tool.min_width(tree) == _parser_width(pattern, flags), f"min_width of {pattern!r}"
    assert tool.min_width(tool.lex("x*\\B")) == 0
    assert tool.min_width(tool.lex("(?:\\B|a)b")) == 1
    assert tool.min_width(tool.lex("a|\\B")) == 0


# ---------------------------------------------------------------------------------------------
# The construct table on Python
# ---------------------------------------------------------------------------------------------


def _probe_observable(pattern: str, text: str) -> bool:
    return re.search(pattern, text) is not None


def _construct_fail_reason(row: Mapping[str, Any]) -> str:
    """A fail row's reason: Python validity first, then what Rust does with the construct."""
    compiles = row["python_compiles"]
    minors = compiles.values() if isinstance(compiles, Mapping) else (compiles,)
    if not all(minors):
        return "python-invalid"
    # The constructs Rust compiles with another meaning are the classifier's own set.
    if row["id"] in classify._RUST_REINTERPRETED:
        return f"rust-reinterpreted:{row['id']}"
    return f"rust-rejected:{row['id']}"


def _check_dispositions(row: Mapping[str, Any]) -> None:
    exemplar = row["exemplar"]
    disposition = row["disposition"]
    tree = tool.lex(exemplar)
    if disposition == "fail":
        verdict = _expect(exemplar, "BOOL", "fail")
        assert verdict.reason == _construct_fail_reason(row), f"{row['id']}: {verdict}"
    elif disposition == "lookaround":
        verdict = _verdict(exemplar, "BOOL")
        assert verdict.kind in ("rewrite", "reported"), f"{row['id']}: {verdict}"
    elif disposition == "translate":
        verdict = _expect(exemplar, "BOOL", "rewrite")
        assert verdict.python_text == exemplar, f"{row['id']}: {verdict}"
        assert verdict.rust_text == check.to_rust_text(exemplar, "BOOL", EMPTY), row["id"]
        assert verdict.rust_text == row["rust_text"], f"{row['id']}: {verdict}"
    elif disposition == "portable":
        assert tool.engine_atoms(tree, "BOOL") == [], f"{row['id']} holds an engine atom"
        assert tool.boundary_atoms(tree, "BOOL") == [], f"{row['id']} holds a boundary"
        _expect(exemplar, "BOOL", "as_written")
    elif disposition == "consumer-dependent":
        _expect(exemplar, "BOOL", "rewrite")
    elif disposition == "engine-dependent":
        _check_engine_dependent(row, tree)


def _check_engine_dependent(row: Mapping[str, Any], tree: tool.Node) -> None:
    exemplar = row["exemplar"]
    atoms = tool.engine_atoms(tree, "BOOL")
    boundaries = tool.boundary_atoms(tree, "BOOL")
    assert atoms or boundaries, f"{row['id']}: an engine-dependent exemplar has no atom"
    if not atoms:
        return
    construct, atom = atoms[0]
    near = ((0x41, 0x41),)
    one_off = ((0x41, 0x42),)
    wide = tuple((point, point) for point in range(0x100, 0x100 + 2 * 70, 2))
    equal = _stable_table({("re", atom): near})
    _expect(exemplar, "BOOL", "as_written", table=equal)
    within = _stable_table({("re", atom): near}, {atom: one_off})
    verdict = _expect(exemplar, "BOOL", "rewrite", table=within)
    assert "T-DOM" in verdict.families, f"{row['id']}: {verdict}"
    assert verdict.rust_text is not None and "[" in verdict.rust_text, f"{row['id']}: {verdict}"
    over = _stable_table({("re", atom): near}, {atom: wide})
    _expect(exemplar, "BOOL", "reported", f"{SEMANTIC_PREFIX}re:{construct}", table=over)
    members = tool.basis_members(tool.lex(atom), "BOOL") or ()
    if members:
        unstable = _split_digest(equal, "re", members[0])
        _expect(
            exemplar,
            "BOOL",
            "reported",
            f"{SEMANTIC_PREFIX}re:{construct}-version-difference",
            table=unstable,
        )


def _check_engine_follows_consumer() -> None:
    differing = ((0x09, 0x0D),)
    rust_set = ((0x09, 0x0D), (0x20, 0x20))
    table = _stable_table({("re", "\\s"): differing, ("regex", "\\s"): rust_set}, {"\\s": rust_set})
    verdict = _expect("\\s", "BOOL", "rewrite", table=table)
    assert "T-DOM" in verdict.families, verdict
    _expect("\\s", "SEARCH-PKG-M", "as_written", table=table)
    lacking = _replace(
        table, python={key: value for key, value in table.python.items() if key[0] == "re"}
    )
    with pytest.raises(ValueError):
        check.classify("\\s", "SEARCH-PKG-M", lacking)


def _check_domain_rows(document: Mapping[str, Any]) -> None:
    for row in document["domain"]:
        low, high = row["range"]
        for engine, key in DOMAIN_MEMBER_KEYS:
            measured = check.python_membership(row["exemplar"], engine)
            assert _cut(measured, low, high) == row[key], f"{row['id']}: {engine} members"


def _check_boundary_rows(document: Mapping[str, Any]) -> None:
    rows = {row["atom"]: row for row in document["boundary"]}
    assert set(BOUNDARY_ATOM_TEXTS) <= set(rows), f"boundary atoms: {sorted(rows)}"
    subjects = {subject for group in tool.BOUNDARY_SUBJECTS.values() for subject in group}
    for atom, row in rows.items():
        assert subjects <= set(row["offsets"]), f"{row['id']}: subjects"
        for subject, engines in row["offsets"].items():
            for engine in ("re", "regex"):
                value = engines[engine]
                assert _version_keys_complete(value), f"{row['id']} {subject!r}: version keys"
                assert list(check.python_boundary(atom, engine, subject)) == _version(value), (
                    f"{row['id']}: {engine} offsets on {subject!r}"
                )
    empty = rows["\\B"]["offsets"][""]["re"]
    assert _version(empty) == ([] if RUNNING_MINOR == "3.13" else [0]), "\\B on the empty subject"


def _check_membership(document: Mapping[str, Any]) -> None:
    membership = document["membership"]
    shorthand = membership["shorthand"]
    basis = tool.MEMBERSHIP_BASIS["shorthand"]
    assert isinstance(basis, tuple)
    expected_atoms = {
        (engine, atom)
        for engine in ("re", "regex")
        for plain in basis
        for atom in (plain, f"(?i:{plain})")
    }
    keys = [(row["engine"], row["atom"]) for row in shorthand]
    assert sorted(keys) == sorted(expected_atoms), "shorthand rows"
    for row in shorthand:
        assert _version_keys_complete(row["digest"]), f"{row['atom']}: version keys"
        ranges = check.python_membership(row["atom"], row["engine"])
        expected = [len(_points(ranges)), check.membership_digest(ranges)]
        assert _version(row["digest"]) == expected, f"{row['engine']} {row['atom']} digest"
    blocks = membership["ignore_case_ascii"]
    assert sorted(row["engine"] for row in blocks) == ["re", "regex"], "block rows"
    bounds = tool.MEMBERSHIP_BASIS["ignore_case_literals"]
    assert isinstance(bounds, tuple) and len(bounds) == 2, bounds
    low, high = int(bounds[0]), int(bounds[1])
    for row in blocks:
        assert row["codepoints"] == [low, high], "block code points"
        assert _version_keys_complete(row["block_sha256"]), "block version keys"
        digests = [
            check.membership_digest(check.python_membership(f"(?i:\\x{point:02x})", row["engine"]))
            for point in range(low, high + 1)
        ]
        assert _sha_text("\n".join(digests)) == _version(row["block_sha256"]), "block digest"
    compositions = membership["composition"]
    assert len(compositions) == 12, "six composition exemplars per engine"
    for row in compositions:
        measured = check.python_membership(row["atom"], row["engine"])
        digest = _version(row["digest"])
        assert isinstance(digest, list) and len(digest) == 2, f"{row['atom']}: digest {digest}"
        assert digest[1] == check.membership_digest(measured), row["atom"]
        node = tool.lex(row["atom"])
        items = node.detail.get("items", ())
        assert isinstance(items, Sequence), f"{row['atom']}: class items"
        composed: set[int] = set()
        for item in items:
            composed |= _points(check.python_membership(str(item), row["engine"]))
        if node.detail.get("negated"):
            composed = _points(_complement(_ranges(sorted(composed))))
        assert composed == _points(measured), f"{row['atom']}: composition"
    assert check.membership_digest([(48, 57)]) == _sha_text("[[48,57]]")
    assert tool.basis_members(tool.lex("\\d"), "BOOL") == ("\\d",)
    assert tool.basis_members(tool.lex("[\\d_]"), "BOOL") == ("\\d",)
    assert tool.basis_members(tool.lex("[a-c]"), "BOOL") == ()
    assert tool.basis_members(tool.lex("(?i:k)"), "BOOL") == ("(?i:\\x6b)",)
    assert tool.basis_members(tool.lex("(?i:\u00b7)"), "BOOL") is None
    assert tool.basis_members(tool.lex("\\b"), "BOOL") == ("\\w",)
    assert tool.basis_members(tool.lex("k"), "SPAN-ALLOWLIST") == ("(?i:\\x6b)",)


def _literal_replacement(text: str) -> Callable[[re.Match[str]], str]:
    def replace(_match: re.Match[str]) -> str:
        return text

    return replace


def _check_iteration(document: Mapping[str, Any]) -> None:
    for row in document["iteration"]:
        spans = [list(m.span()) for m in re.finditer(row["pattern"], row["subject"])]
        assert spans == row["python"], f"{row['id']}: finditer spans"
        replaced = re.sub(row["pattern"], _literal_replacement(row["replacement"]), row["subject"])
        assert replaced == row["python_replaced"], f"{row['id']}: re.sub"


def _check_python_validity(document: Mapping[str, Any]) -> None:
    rows = document["constructs"]
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        assert isinstance(row["disposition"], str), f"{row['id']}: disposition is keyed"
        assert _version_keys_complete(row["python_compiles"]), f"{row['id']}: compile keys"
        compiles = row["python_compiles"]
        values = (
            [compiles[minor] for minor in check.SUPPORTED_PYTHON_MINORS]
            if isinstance(compiles, dict)
            else [compiles]
        )
        if not all(values):
            assert row["disposition"] == "fail", f"{row['id']}: invalid somewhere, not fail"
    assert by_id["end-of-string-z"]["python_compiles"] == {"3.13": False, "3.14": True}
    assert check.classify("a\\z", "BOOL", EMPTY) == _fail_python_invalid()
    variants: list[object] = [
        {"3.13": False, "3.14": True},
        {"3.13": True, "3.14": False},
        False,
        {"3.13": True},
    ]
    expected = [("3.13",), ("3.14",), ("3.13", "3.14"), ("3.14",)]
    for variant, minors in zip(variants, expected, strict=True):
        changed = copy.deepcopy(rows)
        for row in changed:
            if row["id"] == "start-A":
                row["python_compiles"] = variant
        assert check.python_invalid_minors("\\Aq", changed) == minors, f"start-A {variant}"
        with mock.patch.object(check, "construct_rows", return_value=tuple(changed)):
            assert check.classify("\\Aq", "BOOL", EMPTY) == _fail_python_invalid(), variant
    assert check.python_invalid_minors("\\Aq", rows) == ()
    assert _verdict("\\Aq", "BOOL").kind != "fail", "start-A with the committed rows"
    assert check.python_invalid_minors("a\\z", check.construct_rows()) == ("3.13",)


def test_construct_table_holds_on_python() -> None:
    document = _load(CONSTRUCTS_FIXTURE)
    constructs = document["constructs"]
    ids = {row["id"] for row in constructs}
    missing = [required for required in REQUIRED_CONSTRUCT_IDS if required not in ids]
    assert missing == [], f"required construct ids are missing: {missing}"
    for row in constructs:
        assert row["disposition"] in DISPOSITIONS, f"{row['id']}: {row['disposition']!r}"
        compiles = row["python_compiles"]
        try:
            re.compile(row["exemplar"])
            compiled = True
        except re.error:
            compiled = False
        assert compiled == _version(compiles), f"{row['id']}: re.compile"
        for probe in row.get("probes", []):
            if not _has_version(probe["python"]):
                continue
            observed = _probe_observable(row["exemplar"], probe["input"])
            assert observed == _version(probe["python"]), f"{row['id']} on {probe['input']!r}"
        _check_dispositions(row)
    _check_engine_follows_consumer()
    _check_domain_rows(document)
    _check_boundary_rows(document)
    _check_membership(document)
    _check_iteration(document)
    _check_python_validity(document)


def test_the_reinterpreted_set_the_audit_reads_is_the_classifiers_fail_constructs() -> None:
    # The audit holds no copy of the set: an id the classifier adds or drops changes the
    # reason _construct_fail_reason expects, so the set must name fail constructs only.
    reinterpreted = classify._RUST_REINTERPRETED
    fail_ids = {
        row["id"] for row in _load(CONSTRUCTS_FIXTURE)["constructs"] if row["disposition"] == "fail"
    }
    outside = sorted(reinterpreted - fail_ids)
    assert reinterpreted and outside == [], (
        f"the classifier's reinterpreted set {sorted(reinterpreted)} names ids that are not"
        f" fail constructs: {outside}"
    )


# ---------------------------------------------------------------------------------------------
# Shape families and their counterexamples
# ---------------------------------------------------------------------------------------------


T_CONSTRUCT_IDS = frozenset(
    {
        "end-of-string-Z",
        "escaped-angle",
        "class-intersection",
        "class-difference",
        "class-symmetric",
        "bracket-in-class",
    }
)


def _sre_anchors(pattern: str, flags: int = 0) -> set[str]:
    """The names of the AT anchors Python's parser finds anywhere in the pattern."""
    parser = importlib.import_module("re._parser")
    constants = importlib.import_module("re._constants")
    found: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, parser.SubPattern):
            for op, argument in value:
                if op is constants.AT:
                    found.add(str(argument))
                walk(argument)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)

    walk(parser.parse(pattern, flags))
    return found


def _holds_reference_construct(original: str) -> bool:
    """Whether a pattern holds `$` or a construct only the T-* translations handle."""
    dollar = bool({"AT_END", "AT_END_LINE"} & _sre_anchors(original))
    return dollar or bool(T_CONSTRUCT_IDS & set(check.rust_safe(original)))


def _fold_tables(original: str) -> tuple[check.DomainTable, check.DomainTable]:
    tree = tool.lex(original)
    python = {
        ("re", atom): check.python_membership(atom, "re")
        for _, atom in tool.engine_atoms(tree, "BOOL")
    }
    stable = _stable_table(python)
    return stable, _split_digest(stable, "re", _hex_literal("a"))


def _check_fold_case(case: Mapping[str, Any]) -> None:
    stable, unstable = _fold_tables(case["original"])
    first = _expect(case["original"], "BOOL", "reported", table=stable)
    second = _expect(
        case["original"], "BOOL", "reported", "lookaround-fold-not-version-stable", table=unstable
    )
    assert any(item.endswith("-version-difference") for item in second.decisions), second
    if case["id"] == "r5-fold-overlap":
        assert first.reason == "lookaround-overlapping-literals", first
    else:
        assert first.reason == "lookaround-fold-not-version-stable", first
        assert any(item.endswith("-version-difference") for item in first.decisions), first


def test_shape_families_hold_on_python_exhaustively() -> None:
    document = _load(SHAPES_FIXTURE)
    cases = document["cases"]
    fold_cases = []
    for case in cases:
        consumer = case["consumer"]
        original = case["original"]
        tree = tool.lex(original)
        # The atoms the original itself holds: the fold cases are classified under BOOL, and an
        # external flag of the case's consumer (SPAN-ALLOWLIST's i) adds atoms no fixture names.
        atoms = tool.engine_atoms(tree, "BOOL") + [
            (construct, atom) for construct, atom, _ in tool.boundary_atoms(tree, "BOOL")
        ]
        if atoms:
            assert "verdict" in case, f"{case['id']}: an engine atom outside a reported case"
            fold_cases.append(case)
            _check_fold_case(case)
        elif "verdict" in case:
            _expect(original, consumer, case["verdict"], case.get("reason"))
        else:
            verdict = _expect(original, consumer, "rewrite")
            assert verdict.python_text == case["python_text"], f"{case['id']}: {verdict}"
            assert verdict.rust_text == case["rust_text"], f"{case['id']}: {verdict}"
            assert verdict.universal == case.get("universal", False), f"{case['id']}"
            assert case["alphabet"] and isinstance(case["max_len"], int), case["id"]
            assert len(case["alphabet"]) ** case["max_len"] <= 50_000, f"{case['id']}: size"
            inputs = list(check.exhaustive_inputs(case["alphabet"], case["max_len"]))
            assert inputs, f"{case['id']}: no exhaustive input"
            for text in inputs:
                left = check.observable(consumer, original, text)
                right = check.observable(consumer, case["python_text"], text)
                assert left == right, f"{case['id']}: differs at {text!r}"
                if case.get("universal"):
                    assert left is True, f"{case['id']}: universal, but no match at {text!r}"
        if _holds_reference_construct(original):
            assert case.get("reference") != "fancy", f"{case['id']}: fancy for a T-* construct"
            if "verdict" not in case:
                assert case.get("reference") == "expect", f"{case['id']}: reference"
                observed = {item["observable"] for item in case.get("expect", [])}
                assert True in observed, f"{case['id']}: no matching expectation"
                if not case.get("universal"):
                    assert False in observed, f"{case['id']}: no failing expectation"
        for expectation in case.get("expect", []):
            text = expectation["input"]
            assert check.observable(consumer, original, text) == expectation["observable"], (
                f"{case['id']}: original on {text!r}"
            )
            if "python_text" in case:
                assert (
                    check.observable(consumer, case["python_text"], text)
                    == expectation["observable"]
                ), f"{case['id']}: rewrite on {text!r}"
    assert sorted(case["id"] for case in fold_cases) == ["r5-fold-overlap", "r5-fold-unmeasured"]
    families = {case["family"] for case in cases}
    assert set(FAMILY_IDS) <= families, f"families: {sorted(set(FAMILY_IDS) - families)}"
    reasons = {case.get("reason") for case in cases}
    assert set(STRUCTURAL_CASE_REASONS) <= reasons, "a structural reason has no case"
    ids = {case["id"] for case in cases}
    assert {"sub-two-matches", "sub-single-match-proved"} <= ids
    assert tool.single_match_only(tool.lex("\\Aq-([0-9]+)"), "SUB")
    assert not tool.single_match_only(tool.lex("q-([0-9]+)"), "SUB")


def test_family_counterexamples_are_rejected() -> None:
    document = _load(SHAPES_FIXTURE)
    counterexamples = document["counterexamples"]
    assert counterexamples, "no counterexample"
    for item in counterexamples:
        if "python" not in item["engines"]:
            continue
        consumer = item["consumer"]
        inputs = list(check.exhaustive_inputs("abcqxy;\n", 4)) + [item["witness"]]
        found = check.check_rewrite(item["original"], item["wrong"], consumer, inputs)
        assert found is not None, f"{item['original']!r} -> {item['wrong']!r} was accepted"
        left = check.observable(consumer, item["original"], item["witness"])
        right = check.observable(consumer, item["wrong"], item["witness"])
        assert left != right, f"{item['original']!r}: the witness {item['witness']!r} agrees"


# ---------------------------------------------------------------------------------------------
# Consuming rewrites
# ---------------------------------------------------------------------------------------------


def test_consuming_rewrites_go_only_to_boolean_consumers() -> None:
    others = [c for c in tool.CONSUMER_CLASSES if c not in ("BOOL", "INERT-EXEMPTION")]
    for consumer in others:
        for pattern in ("a(?=b)", "(?<=[;])q"):
            _expect(pattern, consumer, "reported", "lookaround-non-boolean")
    for consumer in ("FULLMATCH", "FULLMATCH-PATH", "MATCH-GROUPS-FIRSTLINE", "SEARCH-PKG-M"):
        _expect("ab$", consumer, "as_written")
    for consumer in ("SPAN-ALLOWLIST", "SEARCH-GROUP", "SUB", "UNKNOWN"):
        _expect("ab$", consumer, "reported", "dollar-before-final-newline")
    for pattern in ("a(?=b)", "(?<=[;])q", "ab$"):
        _expect(pattern, "BOOL", "rewrite")
    for pattern in ("a(?=b)", "(?<=[;])q", "ab$", "q\\Z"):
        verdict = _verdict(pattern, "UNKNOWN")
        assert not set(verdict.families) & set(CONSUMING_FAMILIES), verdict
    verdict = _expect("q\\Z", "UNKNOWN", "rewrite")
    assert verdict.families == ("T-END",), verdict
    _expect("syn", "INERT-EXEMPTION", "reported", "inert-no-command-pattern")


# ---------------------------------------------------------------------------------------------
# Consumer cases through production
# ---------------------------------------------------------------------------------------------


class _FlaggedModule:
    """A regex module whose matching calls carry one extra flag; every other attribute passes."""

    def __init__(self, module: Any, flag: int) -> None:
        self._module = module
        self._flag = flag

    def __getattr__(self, name: str) -> Any:
        original = getattr(self._module, name)
        if name not in FLAG_ARGUMENT_INDEX:
            return original
        index = FLAG_ARGUMENT_INDEX[name]

        def flagged(*args: Any, **kwargs: Any) -> Any:
            if len(args) > index:
                values = list(args)
                values[index] = values[index] | self._flag
                return original(*values, **kwargs)
            kwargs["flags"] = kwargs.get("flags", 0) | self._flag
            return original(*args, **kwargs)

        return flagged


@contextlib.contextmanager
def _extra_flag(driver: str, flag: str) -> Iterator[None]:
    module_name, attribute = FLAG_GUARD_PATCH_SITES[driver]
    module = importlib.import_module(module_name)
    engine = getattr(module, attribute)
    value = engine.IGNORECASE if flag == "case_insensitive" else engine.MULTILINE
    with mock.patch.object(module, attribute, _FlaggedModule(engine, value)):
        yield


def _control_case(case: Mapping[str, Any]) -> dict[str, Any]:
    """The case as its control runs: the control's own pattern, effect or regex switch."""
    control = dict(case)
    for key, replaced in (
        ("control_pattern", "pattern"),
        ("control_effect", "effect"),
        ("regex_control", "regex"),
    ):
        if key in case:
            control[replaced] = case[key]
    return control


def _side_text(case: Mapping[str, Any], side: str) -> str:
    """The side's subject; a nested case's driver builds both subjects from the side name."""
    return str(case[side]) if side in case else side


def _drive(case: Mapping[str, Any], side: str, tmp: Path, pattern: str | None = None) -> object:
    run = _control_case(case) if side == "control" else case
    return check.drive_consumer(run, _side_text(case, side), pattern=pattern, tmp=tmp)


def test_consumer_cases_reach_the_production_consumer(tmp_path: Path) -> None:
    document = _load(CONSUMERS_FIXTURE)
    cases = document["cases"]
    classes = {case["consumer"] for case in cases}
    wanted = set(tool.CONSUMER_CLASSES) - {"UNKNOWN"}
    assert wanted <= classes, f"classes without a case: {sorted(wanted - classes)}"
    for case in cases:
        input_value = _drive(case, "input", tmp_path)
        control_value = _drive(case, "control", tmp_path)
        assert input_value != control_value, f"{case['id']}: input and control agree"
        assert input_value == case["expect"]["input"], f"{case['id']}: input {input_value!r}"
        assert control_value == case["expect"]["control"], f"{case['id']}: control"
        if case.get("rewrite_check"):
            assert case["consumer"] == "BOOL", f"{case['id']}: rewrite_check outside BOOL"
            rewritten = _verdict(case["pattern"], "BOOL").python_text
            assert rewritten is not None and rewritten != case["pattern"], case["id"]
            assert _drive(case, "input", tmp_path, rewritten) == input_value, case["id"]
            assert _drive(case, "control", tmp_path, rewritten) == control_value, case["id"]
        if "wrong_rewrite" in case:
            wrong = _drive(case, "input", tmp_path, case["wrong_rewrite"])
            assert wrong != case["expect"]["input"], f"{case['id']}: the wrong form agrees"
    for consumer in WITHOUT_CONSUMER_CALL:
        assert consumer not in tool.CONSUMERS.values(), f"{consumer} names a consumer"
    pairs = {
        (consumer, flag)
        for consumer in tool.CONSUMER_CLASSES
        if consumer not in WITHOUT_CONSUMER_CALL
        for flag in FLAGS
        if flag not in tool.CONSUMER_FLAGS[consumer]
    }
    guards = {(case["consumer"], case["flag_guard"]) for case in cases if "flag_guard" in case}
    assert not guards & set(FLAG_GUARD_EXEMPTIONS), "a guarded pair is also exempt"
    assert pairs == guards | set(FLAG_GUARD_EXEMPTIONS), (
        f"flag guards: missing {sorted(pairs - guards - set(FLAG_GUARD_EXEMPTIONS))}, "
        f"extra {sorted((guards | set(FLAG_GUARD_EXEMPTIONS)) - pairs)}"
    )
    for case in cases:
        if "flag_guard" not in case:
            continue
        driver = case["driver"]
        assert driver in FLAG_GUARD_PATCH_SITES, f"{case['id']}: no patch site for {driver}"
        module_name, attribute = FLAG_GUARD_PATCH_SITES[driver]
        assert getattr(importlib.import_module(module_name), attribute) in (re, regex), driver
        with _extra_flag(driver, case["flag_guard"]):
            flagged_control = _drive(case, "control", tmp_path)
            flagged_input = _drive(case, "input", tmp_path)
        assert flagged_control != case["expect"]["control"], f"{case['id']}: no guard"
        assert flagged_input == case["expect"]["input"], f"{case['id']}: the input changed"
    assert sorted(FLAG_GUARD_EXEMPTIONS.values()) == [
        "no-anchor-in-allowlist",
        "single-line-subject",
    ]
    assert tool.SUBJECT_DOMAINS["MATCH-GROUPS-FIRSTLINE"] == "scalar-first-line"
    for pattern in ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS:
        assert tool.flag_inert(tool.lex(pattern), "SPAN-ALLOWLIST", "multi_line"), pattern


# ---------------------------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------------------------


def _row_message(item: tool.FoundRegex, verdict: check.Verdict, detail: str = "") -> str:
    return (
        f"{item.source} {item.location} {item.pattern!r} ({item.consumer}): "
        f"{verdict.kind} {verdict.reason} rust_text={verdict.rust_text!r} {detail}"
    )


def _unstable_atoms(
    item: tool.FoundRegex, membership: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    """Atoms not stable by their digest rows or basis, and those unstable by composition only."""
    tree = _lexed(item.pattern)
    if tree is None:
        return [], []
    engine = tool.CONSUMER_ENGINES[item.consumer]
    by_rows: list[str] = []
    texts = [atom for _, atom in tool.engine_atoms(tree, item.consumer)]
    texts += [atom for _, atom, _ in tool.boundary_atoms(tree, item.consumer)]
    for atom in texts:
        members = tool.basis_members(tool.lex(atom), item.consumer)
        if members is None:
            by_rows.append(atom)
            continue
        for basis in members:
            values = [
                _fixture_digest(membership, engine, basis, minor)
                for minor in check.SUPPORTED_PYTHON_MINORS
            ]
            if any(v is None for v in values) or len({json.dumps(v) for v in values}) != 1:
                by_rows.append(atom)
                break
    return by_rows, texts


def _gate_stability(
    found: Sequence[tool.FoundRegex],
    rows: Sequence[tuple[tool.FoundRegex, check.Verdict]],
    after: Sequence[tuple[tool.FoundRegex, check.Verdict]],
    domain: check.DomainTable,
    membership: Mapping[str, Any],
) -> None:
    for index, (item, verdict) in enumerate(rows):
        unstable, texts = _unstable_atoms(item, membership)
        engine = tool.CONSUMER_ENGINES[item.consumer]
        for atom in texts:
            stable = _recomputed_stable(item.consumer, atom, membership, domain)
            assert stable == domain.version_stable(item.consumer, tool.lex(atom)), _row_message(
                item, verdict, f"stability of {atom!r}"
            )
            if stable:
                continue
            assert verdict.kind == "reported", _row_message(item, verdict, f"unstable {atom!r}")
            ids = [d for d in verdict.decisions if d.endswith("-version-difference")]
            assert ids, _row_message(item, verdict, "no version id in decisions")
            if verdict.reason not in EARLY_REASONS and not str(verdict.reason).startswith(
                "lookaround-"
            ):
                assert verdict.reason == f"{SEMANTIC_PREFIX}{ids[0]}", _row_message(item, verdict)
        if not unstable:
            continue
        python_empty = dict(domain.python)
        python_full = dict(domain.python)
        for atom in unstable:
            python_empty[(engine, atom)] = NO_SCALARS
            python_full[(engine, atom)] = ALL_SCALARS
        for python in (python_empty, python_full):
            replaced = check.classify(item.pattern, item.consumer, _replace(domain, python=python))
            assert replaced == verdict, _row_message(item, verdict, f"follows sets: {replaced}")
            assert after[index][1] == replaced, _row_message(item, verdict, "verify changed it")


def _gate_verify(
    rows: Sequence[tuple[tool.FoundRegex, check.Verdict]],
    after: Sequence[tuple[tool.FoundRegex, check.Verdict]],
) -> None:
    assert len(after) == len(rows), f"verify returned {len(after)} rows for {len(rows)}"
    assert [item for item, _ in after] == [item for item, _ in rows], "verify reordered rows"
    for (item, before), (_, verified) in zip(rows, after, strict=True):
        assert verified.reason != "unverified-proposal", _row_message(item, verified)
        if before.kind == "reported":
            assert verified == before, _row_message(item, verified, "verify changed a report row")
        elif before.kind in ("as_written", "rewrite"):
            ok = verified.verified and verified.kind == before.kind
            mismatch = verified.kind == "fail" and str(verified.reason).startswith(
                "two-engine-mismatch"
            )
            assert ok or mismatch, _row_message(item, verified)


def _gate_class_checks(item: tool.FoundRegex, verdict: check.Verdict) -> None:
    tree = tool.lex(item.pattern)
    probes = check.derive_probes(item.pattern, verdict.python_text or item.pattern, item.consumer)
    entry = check.work_entry(0, verdict.rust_text or item.pattern, item.consumer, probes)
    flags = {flag: flag in tool.CONSUMER_FLAGS[item.consumer] for flag in FLAGS}
    assert entry.get("flags") == flags, _row_message(item, verdict, f"entry flags {entry}")
    exact = verdict.kind in ("as_written", "rewrite")
    if item.consumer == "SUB":
        assert entry.get("mode") == "sub", _row_message(item, verdict, "sub mode")
        assert entry.get("replacement") == tool.SUB_REPLACEMENT, _row_message(item, verdict)
        if tool.min_width(tree) == 0:
            assert verdict.reason == "sub-empty-match-iteration", _row_message(item, verdict)
        if exact:
            assert tool.min_width(tree) >= 1, _row_message(item, verdict)
            multi = any(
                len(_sub_matches(item.consumer, item.pattern, probe)) >= 2 for probe in probes
            )
            assert multi or tool.single_match_only(tree, item.consumer), _row_message(item, verdict)
    for consumer, flag in (("SPAN-ALLOWLIST", "case_insensitive"), ("SEARCH-PKG-M", "multi_line")):
        if item.consumer != consumer:
            continue
        assert flags[flag], _row_message(item, verdict, f"{flag} not set")
        if exact:
            witnessed = any(
                check.observable(consumer, item.pattern, probe)
                != check.observable(consumer, item.pattern, probe, without=flag)
                for probe in probes
            )
            inert = (
                _recomputed_case_inert(tree, consumer)
                if flag == "case_insensitive"
                else _no_line_anchor(item.pattern)
            )
            assert witnessed or inert, _row_message(item, verdict, f"no {flag} witness")


def _sub_matches(consumer: str, pattern: str, probe: str) -> Sequence[object]:
    value = check.observable(consumer, pattern, probe)
    return value[0] if isinstance(value, tuple) and value else ()


def _recomputed_case_inert(tree: tool.Node, consumer: str) -> bool:
    """Each literal and class atom has one membership with and without ``i`` on the engine.

    An atom the pattern already folds inline cannot change when the flag is added outside.
    """
    engine = tool.CONSUMER_ENGINES[consumer]
    source = tool._source(tree)
    for node in tool.atom_nodes(tree, consumer):
        if node.detail.get("ci"):
            continue
        atom = tool.atom_text(node, consumer, source)
        bare = re.sub(r"^\(\?i:(.*)\)$", r"\1", atom)
        if check.python_membership(atom, engine) != check.python_membership(bare, engine):
            return False
    return True


def _no_line_anchor(pattern: str) -> bool:
    """No ``^`` or ``$`` that the external ``m`` flag would change (an inline ``m`` already holds)."""
    return not any(
        node.kind == "anchor" and node.detail["kind"] in ("^", "$") and not node.detail["multiline"]
        for node in tool.walk(tool.lex(pattern))
    )


def _gate_boundaries(item: tool.FoundRegex, verdict: check.Verdict) -> None:
    tree = tool.lex(item.pattern)
    sites = tool.boundary_atoms(tree, item.consumer)
    if not sites:
        return
    assert len(verdict.boundaries) == len(sites), _row_message(item, verdict, "boundary rows")
    exact = verdict.kind in ("as_written", "rewrite")
    for (_, _, alternative), entry in zip(sites, verdict.boundaries, strict=True):
        conditions = [entry.get(group) for group in tool.BOUNDARY_SUBJECTS]
        if exact:
            assert entry.get("membership") in ("equal", "accepted"), _row_message(item, verdict)
            assert entry.get("nonword_only") == "equal", _row_message(item, verdict)
            assert entry.get("word_edge") == "equal", _row_message(item, verdict)
            assert entry.get("empty") in ("equal", "restricted:nonempty-alternative"), _row_message(
                item, verdict
            )
            if entry.get("empty") == "restricted:nonempty-alternative":
                assert tool.min_width(alternative) >= 1, _row_message(item, verdict)
        if "decision" in conditions:
            assert verdict.kind == "reported", _row_message(item, verdict)
            assert str(verdict.reason).startswith(SEMANTIC_PREFIX), _row_message(item, verdict)


def test_every_universe_regex_meets_the_portability_property() -> None:
    for root, _ in tool.UNIVERSE_ROOTS:
        assert root.is_dir(), f"{root} is not a directory"
    independent = _independent_universe()
    found = tool.scan_universe()
    observed = Counter((item.source, item.location, item.pattern) for item in found)
    assert observed == Counter(independent), (
        "scan_universe differs from the independent pass: "
        f"missing {sorted((Counter(independent) - observed).elements())[:5]}, "
        f"extra {sorted((observed - Counter(independent)).elements())[:5]}"
    )
    assert found or not independent, "the independent pass found patterns, the scan none"
    found, domain, constructs = _live()
    membership = constructs["membership"]
    rows = [(item, check.classify(item.pattern, item.consumer, domain)) for item in found]
    after = check.verify(rows)
    _gate_verify(rows, after)
    _gate_stability(found, rows, after, domain, membership)
    references: dict[str, check.Verdict] = {}
    for item, verdict in after:
        if item.source == tool.PYTHON_CONSTANT_SOURCE:
            references[item.pattern] = verdict
    for item, verdict in after:
        assert verdict.kind != "fail", _row_message(item, verdict)
        assert verdict.kind in VERDICT_KINDS, _row_message(item, verdict)
        _gate_class_checks(item, verdict)
        _gate_boundaries(item, verdict)
        if verdict.kind in ("as_written", "rewrite"):
            assert verdict.verified is True, _row_message(item, verdict)
            assert verdict.decisions == (), _row_message(item, verdict)
            assert check.rust_safe(str(verdict.rust_text)) == [], _row_message(item, verdict)
        if verdict.kind == "rewrite" and set(verdict.families) & set(CONSUMING_FAMILIES):
            text = str(verdict.python_text)
            probes = check.derive_probes(item.pattern, text, item.consumer)
            assert check.check_rewrite(item.pattern, text, item.consumer, probes) is None
            truths = [check.observable(item.consumer, item.pattern, p) for p in probes]
            if verdict.universal:
                assert check.matches_everything(text, item.consumer), _row_message(item, verdict)
                assert all(truths), _row_message(item, verdict)
            else:
                assert True in truths and False in truths, _row_message(item, verdict)
        if verdict.kind == "reported":
            reason = str(verdict.reason)
            if reason.startswith(SEMANTIC_PREFIX):
                assert reason.removeprefix(SEMANTIC_PREFIX) not in tool.ACCEPTED_DIVERGENCES
            else:
                assert reason in REPORT_REASONS, _row_message(item, verdict)
        if item.reference:
            assert verdict == references.get(item.pattern), _row_message(item, verdict)


def test_a_rewrite_that_fails_its_probe_check_fails_the_gate() -> None:
    proved = _expect("a(?=b)", "BOOL", "rewrite")
    assert set(proved.families) & set(CONSUMING_FAMILIES), proved
    with mock.patch.object(check, "check_rewrite", return_value="ab"):
        _expect("a(?=b)", "BOOL", "fail", "rewrite-mismatch:'ab'")
        # A translation-only rewrite has no Python rewrite to check, so the probe check is not run.
        translated = _expect("q\\Z", "UNKNOWN", "rewrite")
    assert translated.families == ("T-END",), translated


def _fail_reason(pattern: str) -> str | None:
    verdict = _verdict(pattern, "BOOL")
    return verdict.reason if verdict.kind == "fail" else f"not fail: {verdict}"


def test_fail_reasons_name_the_cause_from_the_closed_set() -> None:
    with pytest.raises(tool.UnknownConstruct) as unknown:
        tool.lex("[a")
    assert str(unknown.value) == "[a"
    # The lexer runs before validity, so a Python-invalid unknown construct reads as unknown.
    assert _fail_reason("[a") == "unknown-construct:[a"
    assert _fail_reason("(a)\\1") == "rust-rejected:backreference"
    assert _fail_reason("a{,2}") == "rust-rejected:brace-comma-n"
    assert _fail_reason("a*+a") == "rust-reinterpreted:possessive"
    assert _fail_reason("(?x)[ ]a") == "rust-reinterpreted:verbose-flag"
    assert _verdict("a(b)", "BOOL").kind == "as_written"
    with mock.patch.object(tool, "lex_shape", return_value={"groups": 2}):
        assert _fail_reason("a(b)") == "parser-disagreement"
    with mock.patch.object(tool, "min_width", return_value=0):
        assert _fail_reason("a(b)") == "parser-disagreement"


def test_a_unicode_property_reads_as_reinterpreted_where_python_accepts_it() -> None:
    # Only the regex package compiles a property escape; Rust compiles it too, against its own
    # Unicode tables, so the pattern fails as reinterpreted rather than rejected.
    for pattern in ("q\\p{L}", "q\\P{L}"):
        verdict = _verdict(pattern, "SEARCH-PKG-M")
        assert verdict.kind == "fail", verdict
        assert verdict.reason == "rust-reinterpreted:unicode-property", verdict
    # The re module rejects the escape, so Python validity decides first there.
    assert _fail_reason("q\\p{L}") == "python-invalid"


# ---------------------------------------------------------------------------------------------
# The report and its record
# ---------------------------------------------------------------------------------------------

REPORT_DEFINITION = """\
rules:
  - name: synthetic-rx-report
    effects:
      - type: block
        command_pattern: "q+"
      - type: block
        command_pattern: "a(?=b)"
      - type: block
        command_pattern: "a(?!bc)d"
      - type: block
        command_not_pattern: "syn"
      - type: block
        command_pattern: "x*\\\\B"
      - type: block
        command_pattern: "q\\\\d"
      - type: block
        command_pattern: "(?i)q(?=.*a\u00b7)(?=.*b)"
    when: "assistant_response_matches_any(['not slow'], regex=true)"
    message: "{{ t | regex_replace('syn-[0-9]+', 'n') }}"
"""
REPORT_COLUMNS = ("file", "key", "pattern", "consumer class", "why", "options")


def _report_table() -> check.DomainTable:
    word = ((48, 57), (65, 90), (95, 95), (97, 122))
    literals = {
        ("re", f"(?i:{c})"): ((ord(c.upper()), ord(c.upper())), (ord(c), ord(c))) for c in "qab"
    }
    base = _stable_table(
        {
            ("re", "\\w"): word,
            ("re", "\\d"): ((48, 57),),
            ("re", "(?i:\u00b7)"): ((0xB7, 0xB7),),
            **literals,
        },
        python_boundary={
            ("re", minor, "\\B"): {
                "": () if minor == "3.13" else (0,),
                "!": (0, 1),
                "!!": (0, 1, 2),
                " ": (0, 1),
                "a": (),
                "a!": (2,),
                "!a": (0,),
                "ab": (1,),
            }
            for minor in check.SUPPORTED_PYTHON_MINORS
        },
        rust_boundary={
            "\\B": {
                "": (0,),
                "!": (0, 1),
                "!!": (0, 1, 2),
                " ": (0, 1),
                "a": (),
                "a!": (2,),
                "!a": (0,),
                "ab": (1,),
            }
        },
    )
    return _split_digest(base, "re", "\\d")


def test_report_generator_lists_every_reported_regex(tmp_path: Path) -> None:
    root = tmp_path / "definitions"
    root.mkdir()
    (root / "synthetic.yaml").write_text(REPORT_DEFINITION)
    found = tool.scan_universe(roots=[(root, ("*.yaml",))], repo=tmp_path, include_constants=False)
    table = _report_table()
    rows = [(item, check.classify(item.pattern, item.consumer, table)) for item in found]
    json_path = tmp_path / "regex-audit.json"
    md_path = tmp_path / "regex-audit.md"
    questions = tmp_path / "questions.md"
    tool.write_report(rows, json_path, md_path, questions, domain=table)
    for path in (json_path, md_path, questions):
        assert path.is_file(), f"{path} was not written"
    report = json.loads(json_path.read_text())
    report_rows = report["rows"]
    assert len(report_rows) == len(found), "one row per found pattern"
    keys = [(row["source"], row["location"], row["pattern"]) for row in report_rows]
    assert keys == sorted(keys), "rows are not sorted"
    required = {
        "verdict",
        "families",
        "rust_text",
        "reason",
        "atoms",
        "decisions",
        "verified",
        "universal",
        "flags",
        "mode",
        "boundaries",
    }
    by_pattern = {row["pattern"]: row for row in report_rows}
    for row in report_rows:
        assert required <= set(row), f"{row['pattern']!r}: missing {required - set(row)}"
        assert set(row["flags"]) == set(FLAGS), f"{row['pattern']!r}: flags"
    assert by_pattern["syn-[0-9]+"]["mode"] == "sub"
    assert by_pattern["not slow"]["flags"]["case_insensitive"] is True
    assert by_pattern["x*\\B"]["boundaries"], "the boundary row has no boundaries entry"
    fold = by_pattern["(?i)q(?=.*a\u00b7)(?=.*b)"]
    assert fold["reason"] == "lookaround-fold-not-version-stable", fold
    assert fold["rust_text"] is None and "R5" not in fold["families"], fold
    markdown = md_path.read_text()
    for row in report_rows:
        assert row["pattern"] in markdown, f"{row['pattern']!r} is not in the Markdown table"
    decisions = {row["id"]: row for row in report["decisions"]}
    empty_id = "re:non-word-boundary-empty-subject"
    version_id = "re:digit-shorthand-version-difference"
    assert empty_id in decisions and version_id in decisions, sorted(decisions)
    for decision_id in (empty_id, version_id):
        assert decision_id in markdown, f"{decision_id} is not in the Markdown decisions"
        row = decisions[decision_id]
        assert set(check.SUPPORTED_PYTHON_MINORS) <= set(row["python"]), row
        assert "rust" in row, row
    assert "not measured on every supported minor" in json_path.read_text() + markdown
    proposals = [
        row for row in report_rows if row["verdict"] == "reported" and row.get("python_text")
    ]
    assert all(row["verified"] is False for row in proposals)
    if proposals:
        assert "unverified" in markdown, "a proposal is not marked unverified"
    question_text = questions.read_text()
    for column in REPORT_COLUMNS:
        assert column in question_text.lower(), f"questions column {column!r}"
    for row in report_rows:
        listed = row["pattern"] in question_text
        assert listed == (row["verdict"] == "reported"), f"{row['pattern']!r} in questions"
    assert "lookaround-fold-not-version-stable" in question_text
    for phrase in ("allowlist", "lookaround", "replacement template", "basis"):
        assert phrase in question_text.lower(), f"questions lack {phrase!r}"


def test_record_branch_writes_through_the_report_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert CONSUMERS_FIXTURE.is_file(), f"{CONSUMERS_FIXTURE.name} is missing"
    monkeypatch.setenv(RECORD_ENV, "1")
    consumers = tmp_path / "consumers.json"
    shutil.copyfile(CONSUMERS_FIXTURE, consumers)
    document = json.loads(consumers.read_text())
    for case in document["cases"]:
        case.pop("expect", None)
    consumers.write_text(json.dumps(document, indent=2) + "\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    questions = tmp_path / "questions.md"
    check.record_regex_audit(out_dir, questions, consumers)
    for path in (out_dir / "regex-audit.json", out_dir / "regex-audit.md", questions):
        assert path.is_file(), f"{path} was not written"
    recorded = json.loads(consumers.read_text())
    assert all("expect" in case for case in recorded["cases"]), "an expect block is not filled"


def test_rerecord_regex_audit_report() -> None:
    if os.environ.get(RECORD_ENV) != "1":
        pytest.skip("record mode only: set GOBBY_CORPUS_RECORD=1")
    questions = os.environ.get(QUESTIONS_ENV, "")
    assert questions, f"set {QUESTIONS_ENV} to the questions file path in record mode"
    check.record_regex_audit(REPO / "docs" / "port", Path(questions), CONSUMERS_FIXTURE)


# ---------------------------------------------------------------------------------------------
# Data names and the README
# ---------------------------------------------------------------------------------------------


def test_regex_data_names_no_bundled_definition(tmp_path: Path) -> None:
    files = [CONSTRUCTS_FIXTURE, SHAPES_FIXTURE, CONSUMERS_FIXTURE, README]
    for path in files:
        assert path.is_file(), f"{path.name} is missing"
    data = sorted(path for path in FIXTURE_DIR.rglob("*") if path.is_file())
    assert rules_corpus.find_definition_references(data) == []
    roots = {root.relative_to(REPO).as_posix() for root, _ in tool.UNIVERSE_ROOTS}
    assert set(rules_corpus.SCANNED_DIRECTORIES) == roots, "scanned directories"
    named = tmp_path / "named.json"
    named.write_text(json.dumps({"rules": [{"name": "plain-rule", "event": "before_tool"}]}))
    assert rules_corpus.find_definition_references([named]), "an unprefixed rule name passed"
    below = tmp_path / "below.json"
    below.write_text(json.dumps({"path": "src/gobby/install/shared/rules/x.yaml"}))
    assert rules_corpus.find_definition_references([below]), "a bundled path passed"
    rules_file = tmp_path / "rules_file.json"
    rules_file.write_text(json.dumps({"path": ".gobby/workflows/rules/x.yaml"}))
    assert rules_corpus.find_definition_references([rules_file]), "a rules file passed"
    for directory in sorted(roots):
        alone = tmp_path / "alone.json"
        alone.write_text(json.dumps({"paths": [directory, f"{directory}/*.yaml"]}))
        assert rules_corpus.find_definition_references([alone]) == [], directory
    prose = tmp_path / "prose.md"
    prose.write_text("The block reason says go and stop; the synthetic case reads plain words.\n")
    assert rules_corpus.find_definition_references([prose]) == []


README_PHRASES = (
    "GOBBY_CORPUS_RECORD=1",
    "test_regex_audit.py",
    "src/gobby/install/shared",
    ".gobby/workflows/rules",
    "Python engine `re`",
    "Python engine `regex`",
    "case_insensitive",
    "multi_line",
    "fullmatch",
    "sub-empty-match-iteration",
    "no-multi-match-witness",
    "-empty-subject",
    "-version-difference",
    "lookaround-fold-not-version-stable",
    "an absent group is None on the Python side and null in the driver",
    "python-invalid",
    "class_matches_newline",
    "verify",
    "flag_guard",
    "single-line-subject",
    "no-anchor-in-allowlist",
    "docs/port/regex-audit",
    "synthetic-",
    "find_definition_references",
)


def test_regex_readme_states_the_generic_property() -> None:
    assert README.is_file(), f"{README.name} is missing"
    text = README.read_text()
    missing = [phrase for phrase in README_PHRASES if phrase not in text]
    assert missing == [], f"the README does not state: {missing}"
    for consumer in tool.CONSUMER_CLASSES:
        assert consumer in text, f"the README does not name {consumer}"
    for root, _ in tool.UNIVERSE_ROOTS:
        relative = root.relative_to(REPO).as_posix()
        below = re.findall(re.escape(relative) + r"/[^\s`*]+", text)
        assert below == [], f"the README names files below {relative}: {below}"


# ---------------------------------------------------------------------------------------------
# The Rust-safe syntax filter
# ---------------------------------------------------------------------------------------------


def _invalid_somewhere(row: Mapping[str, Any]) -> bool:
    compiles = row["python_compiles"]
    if isinstance(compiles, dict):
        return not all(compiles.values())
    return not compiles


def test_rust_safe_predicate_flags_every_unsafe_construct() -> None:
    document = _load(CONSTRUCTS_FIXTURE)
    excluded = []
    for row in document["constructs"]:
        exemplar = row["exemplar"]
        validity_only = row["rust_compiles"] and _invalid_somewhere(row)
        if validity_only:
            excluded.append(row["id"])
            assert check.rust_safe(exemplar) == [], row["id"]
            continue
        if not row["rust_compiles"] or row["disposition"] in ("translate", "fail"):
            assert row["id"] in check.rust_safe(exemplar), f"{row['id']}: not flagged"
        if row["disposition"] in ("portable", "engine-dependent"):
            assert check.rust_safe(exemplar) == [], row["id"]
        if row["disposition"] == "translate":
            assert check.rust_safe(check.to_rust_text(exemplar, "BOOL", EMPTY)) == [], row["id"]
    assert excluded == ["end-of-string-z"], f"validity-only entries: {excluded}"
    assert check.rust_safe("a\\z") == []
    assert check.rust_safe("a$") == []
    shapes = _load(SHAPES_FIXTURE)
    for case in shapes["cases"]:
        if "verdict" in case:
            continue
        assert check.rust_safe(case["rust_text"]) == [], case["id"]
        if tool.sre_shape(case["original"], 0).get("lookahead", 0) + sum(
            tool.sre_shape(case["original"], 0).get(key, 0)
            for key in ("negative_lookahead", "lookbehind", "negative_lookbehind")
        ):
            assert check.rust_safe(case["original"]) != [], case["id"]


# ---------------------------------------------------------------------------------------------
# The two-engine driver
# ---------------------------------------------------------------------------------------------


def _hand_entry(
    index: int, text: str, mode: str, probes: Sequence[str], *, ci: bool = False, ml: bool = False
) -> dict[str, object]:
    entry: dict[str, object] = {
        "index": index,
        "text": text,
        "flags": {"case_insensitive": ci, "multi_line": ml},
        "mode": mode,
        "probes": list(probes),
    }
    if mode == "sub":
        entry["replacement"] = tool.SUB_REPLACEMENT
    return entry


def _probe(results: check.DriverResults, index: int, probe: int = 0) -> object:
    value = results.patterns[index]
    assert not isinstance(value, Mapping), f"entry {index} has an error: {value}"
    assert isinstance(value, Sequence), f"entry {index}: {value!r}"
    return value[probe]


def test_two_engine_driver_detects_a_known_divergence() -> None:
    sub_text = "id syn-42 syn-7"
    entries: list[dict[str, object]] = [
        check.work_entry(0, "q+", "BOOL", ["q", "x"]),
        check.work_entry(1, "a(?=b)", "BOOL", ["ab"]),
        check.work_entry(2, "[+--]", "BOOL", [","]),
        check.work_entry(3, "[+-\\-]", "BOOL", [","]),
        check.work_entry(4, "syn-([0-9]+)", "SUB", [sub_text]),
        _hand_entry(5, "syn-([0-9]+)", "search", [sub_text]),
        check.work_entry(6, "x*", "SUB", ["abxd"]),
        check.work_entry(7, "(a)?b", "SUB", ["b ab"]),
        check.work_entry(8, "(a)?b", "SEARCH-GROUP", ["b ab"]),
        check.work_entry(9, "not slow", "SPAN-ALLOWLIST", ["It is NOT slow"]),
        _hand_entry(10, "not slow", "search", ["It is NOT slow"]),
        _hand_entry(11, "^b", "search", ["a\nb"], ml=True),
        _hand_entry(12, "^b", "search", ["a\nb"]),
        {"index": 13, "text": "q", "mode": "search", "probes": ["q"]},
        {"index": 14, "text": "q", "flags": {"multi_line": False}, "mode": "search", "probes": []},
        {
            "index": 15,
            "text": "q",
            "flags": {"case_insensitive": False, "multi_line": False, "dot_all": False},
            "mode": "search",
            "probes": ["q"],
        },
        _hand_entry(16, "q", "replace", ["q"]),
        {k: v for k, v in _hand_entry(17, "q", "sub", ["q"]).items() if k != "replacement"},
        check.work_entry(18, "a|ab", "FULLMATCH", ["ab", "abc"]),
        _hand_entry(19, "a|ab", "search", ["ab", "abc"]),
        check.work_entry(20, "b", "MATCH-GROUPS-FIRSTLINE", ["ab"]),
        _hand_entry(21, "b", "search", ["ab"]),
    ]
    atoms = [{"index": 0, "text": "[a-c]"}]
    boundaries = [
        {"index": 0, "text": "\\B", "subjects": ["", "!", "a!"]},
        {"index": 1, "text": "\\b", "subjects": [""]},
    ]
    results = check.run_two_engine_driver(entries, atoms, boundaries)
    assert len(results.patterns) == len(entries), f"{len(results.patterns)} of {len(entries)}"
    assert len(results.atoms) == len(atoms), "atom results"
    assert len(results.boundaries) == len(boundaries), "boundary results"

    q_hit = _probe(results, 0, 0)
    assert q_hit == (True, (0, 1), ()), q_hit
    assert _probe(results, 0, 1) == (False, None, ())
    assert isinstance(results.patterns[1], Mapping) and "error" in results.patterns[1]
    comma = check.observable("BOOL", "[+--]", ",")
    assert comma is True
    assert check.compare_observable("BOOL", comma, _probe(results, 2)) is not None
    assert check.compare_observable("BOOL", comma, _probe(results, 3)) is None
    assert results.atoms[0] == ((97, 99),)
    assert check.python_membership("[a-c]", "re") == ((97, 99),)
    assert check.python_membership("[a-c]", "regex") == ((97, 99),)

    python_sub = check.observable("SUB", "syn-([0-9]+)", sub_text)
    assert python_sub == ((((3, 9), ((7, 9),)), ((10, 15), ((14, 15),))), "id n n"), python_sub
    sub_result = _probe(results, 4)
    assert isinstance(sub_result, tuple) and sub_result[1] == "id n n", sub_result
    assert check.compare_observable("SUB", python_sub, sub_result) is None
    one_match = _probe(results, 5)
    assert one_match == (True, (3, 9), ((7, 9),)), f"search mode is not one match: {one_match!r}"
    assert check.compare_observable("SUB", python_sub, one_match) is not None
    altered = (sub_result[0], "id n syn-7")
    assert check.compare_observable("SUB", python_sub, altered) is not None
    iteration = _probe(results, 6)
    assert isinstance(iteration, tuple) and len(iteration[0]) == 4, iteration
    assert check.compare_observable("SUB", check.observable("SUB", "x*", "abxd"), iteration)

    absent = check.observable("SUB", "(a)?b", "b ab")
    assert absent == ((((0, 1), (None,)), ((2, 4), ((2, 3),))), "n n"), absent
    absent_result = _probe(results, 7)
    assert absent_result == ((((0, 1), (None,)), ((2, 4), ((2, 3),))), "n n"), absent_result
    assert check.compare_observable("SUB", absent, absent_result) is None
    first = re.search("(a)?b", "b ab")
    assert first is not None and first.span(1) == (-1, -1)
    raw = ((((0, 1), ((-1, -1),)), ((2, 4), ((2, 3),))), "n n")
    assert check.compare_observable("SUB", raw, absent_result) is not None
    empty_group = ((((0, 1), ((0, 0),)), ((2, 4), ((2, 3),))), "n n")
    assert check.compare_observable("SUB", absent, empty_group) is not None
    assert check.group_spans(first) == (None,)
    empty_match = re.search("(a*)b", "b")
    assert empty_match is not None and check.group_spans(empty_match) == ((0, 0),)
    search_group = check.observable("SEARCH-GROUP", "(a)?b", "b ab")
    assert search_group == (True, (0, 1), (None,)), search_group
    assert _probe(results, 8) == (True, (0, 1), (None,))
    assert check.compare_observable("SEARCH-GROUP", search_group, _probe(results, 8)) is None

    span = check.observable("SPAN-ALLOWLIST", "not slow", "It is NOT slow")
    assert span == (6, 14), span
    assert check.compare_observable("SPAN-ALLOWLIST", span, _probe(results, 9)) is None
    assert _probe(results, 10) == (False, None, ())
    assert check.compare_observable("SPAN-ALLOWLIST", span, _probe(results, 10)) is not None
    assert _probe(results, 11) == (True, (2, 3), ())
    assert _probe(results, 12) == (False, None, ())

    for index in range(13, 18):
        refused = results.patterns[index]
        assert isinstance(refused, Mapping), f"entry {index} was run: {refused!r}"
        assert str(refused.get("error", "")).startswith("bad entry"), refused

    assert results.boundaries[0] == {"": (0,), "!": (0, 1), "a!": (2,)}
    assert results.boundaries[1] == {"": ()}

    for probe_index, probe in enumerate(("ab", "abc")):
        python_full = check.observable("FULLMATCH", "a|ab", probe)
        full_result = _probe(results, 18, probe_index)
        assert check.compare_observable("FULLMATCH", python_full, full_result) is None, probe
    assert _probe(results, 18, 0) == (True, (0, 2), ())
    assert _probe(results, 18, 1) == (False, None, ())
    python_abc = check.observable("FULLMATCH", "a|ab", "abc")
    assert check.compare_observable("FULLMATCH", python_abc, _probe(results, 19, 1)) is not None
    python_match = check.observable("MATCH-GROUPS-FIRSTLINE", "b", "ab")
    assert _probe(results, 20) == (False, None, ())
    match_result = _probe(results, 20)
    assert check.compare_observable("MATCH-GROUPS-FIRSTLINE", python_match, match_result) is None
    assert _probe(results, 21) == (True, (1, 2), ())
    assert (
        check.compare_observable("MATCH-GROUPS-FIRSTLINE", python_match, _probe(results, 21))
        is not None
    )

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("GOBBY_REGEX_AUDIT_WORK", "GOBBY_REGEX_AUDIT_OUT")
    }
    unset = subprocess.run(
        [
            "cargo",
            "test",
            "--locked",
            "--manifest-path",
            str(REPO / "Cargo.toml"),
            "-p",
            "gobby-port",
            "--test",
            "suite",
            "--",
            "--ignored",
            "--exact",
            "regex_audit::run_generated_patterns",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unset.returncode != 0, "the ignored driver test passed without a work file"
    assert "work file not given" in unset.stdout + unset.stderr


# ---------------------------------------------------------------------------------------------
# Engine-dependent constructs
# ---------------------------------------------------------------------------------------------


def test_engine_dependent_constructs_are_translated_restricted_or_reported() -> None:
    one_off = _stable_table({("re", "\\s"): ((0x09, 0x0D), (0x1C, 0x1C))}, {"\\s": ((9, 13),)})
    verdict = _expect("q\\s", "BOOL", "rewrite", table=one_off)
    assert "T-DOM" in verdict.families and verdict.rust_text, verdict
    assert "1c" in verdict.rust_text.lower() or "\x1c" in verdict.rust_text, verdict
    wide = tuple((point, point) for point in range(0x100, 0x100 + 2 * 70, 2))
    over = _stable_table({("re", "\\s"): ((9, 13),)}, {"\\s": wide})
    _expect("q\\s", "BOOL", "reported", f"{SEMANTIC_PREFIX}re:space-shorthand", table=over)
    with mock.patch.object(tool, "ACCEPTED_DIVERGENCES", frozenset({"re:space-shorthand"})):
        accepted = _expect("q\\s", "BOOL", "as_written", table=over)
        assert "re:space-shorthand" in accepted.atoms, accepted
    split = _stable_table(
        {("re", "\\s"): ((9, 13), (0x1C, 0x1C)), ("regex", "\\s"): ((9, 13), (0x85, 0x85))},
        {"\\s": ((9, 13),)},
    )
    bool_text = _expect("q\\s", "BOOL", "rewrite", table=split).rust_text or ""
    pkg_text = _expect("q\\s", "SEARCH-PKG-M", "rewrite", table=split).rust_text or ""
    assert ("1c" in bool_text.lower() or "\x1c" in bool_text) and "85" not in bool_text.lower()
    assert "85" in pkg_text.lower() or "\x85" in pkg_text, pkg_text
    split_over = _stable_table(
        {("re", "\\s"): ((9, 13),), ("regex", "\\s"): ((9, 13),)}, {"\\s": wide}
    )
    _expect(
        "q\\s",
        "SEARCH-PKG-M",
        "reported",
        f"{SEMANTIC_PREFIX}regex:space-shorthand",
        table=split_over,
    )
    with mock.patch.object(tool, "ACCEPTED_DIVERGENCES", frozenset({"re:space-shorthand"})):
        _expect("q\\s", "SEARCH-PKG-M", "reported", table=split_over)

    assert set(tool.SUBJECT_DOMAINS) == set(tool.CONSUMER_CLASSES)
    assert set(tool.SUBJECT_DOMAINS.values()) <= {"scalar", "scalar-first-line"}
    assert set(tool.CONSUMER_ENGINES) == set(tool.CONSUMER_CLASSES)
    assert set(tool.CONSUMER_ENGINES.values()) <= {"re", "regex"}
    assert tool.CONSUMER_ENGINES["SEARCH-PKG-M"] == "regex"
    compiled = compile_safe_regex("q")
    pattern_object = compiled._compiled
    assert isinstance(pattern_object, regex.Pattern), f"{compiled!r} holds {pattern_object!r}"
    assert pattern_object.flags & regex.MULTILINE

    found, domain, constructs = _live()
    membership = constructs["membership"]
    unequal = [
        row
        for row in membership["shorthand"]
        if isinstance(row["digest"], dict)
        and len({json.dumps(v) for v in row["digest"].values()}) > 1
    ]
    assert unequal, "no fixture row is version-keyed with unequal digests"
    assert domain.version_stable("BOOL", tool.lex("\\d")) is False
    assert domain.version_stable("BOOL", tool.lex("\\s")) is True
    for row in membership["shorthand"]:
        for minor in check.SUPPORTED_PYTHON_MINORS:
            expected = _fixture_digest(membership, row["engine"], row["atom"], minor)
            key = (row["engine"], minor, row["atom"])
            assert isinstance(expected, list)
            assert domain.python_digest.get(key) == expected[1], f"digest {key}"
        if (row["engine"], row["atom"]) in domain.python:
            measured = domain.python[(row["engine"], row["atom"])]
            assert [len(_points(measured)), check.membership_digest(measured)] == _version(
                row["digest"]
            ), f"{row['engine']} {row['atom']}"
    occurrences: list[tuple[str, str, str]] = []
    for item in found:
        tree = _lexed(item.pattern)
        if tree is not None:
            occurrences += [
                (item.consumer, construct, atom)
                for construct, atom in tool.engine_atoms(tree, item.consumer)
            ]
    for row in constructs["constructs"]:
        if row["disposition"] == "engine-dependent":
            tree = tool.lex(row["exemplar"])
            for consumer in ("BOOL", "SEARCH-PKG-M"):
                occurrences += [
                    (consumer, construct, atom)
                    for construct, atom in tool.engine_atoms(tree, consumer)
                ]
    for consumer, construct, atom in sorted(set(occurrences)):
        engine = tool.CONSUMER_ENGINES[consumer]
        measured = check.python_membership(atom, engine)
        assert domain.python_for(consumer, atom) == measured, f"{engine} {atom}"
        stable = _recomputed_stable(consumer, atom, membership, domain)
        verdict = check.classify(atom, consumer, domain)
        assert isinstance(verdict, check.Verdict)
        if not stable:
            assert verdict.kind == "reported", f"{atom!r} {consumer}: {verdict}"
            assert "T-DOM" not in verdict.families, f"{atom!r} {consumer}: {verdict}"
            assert f"{engine}:{construct}-version-difference" in verdict.decisions, verdict
            continue
        rust = domain.rust.get(atom)
        if rust == measured:
            assert verdict.kind == "as_written", f"{atom!r} {consumer}: {verdict}"
        elif verdict.kind == "rewrite":
            assert "T-DOM" in verdict.families, verdict
            text = str(verdict.rust_text)
            translated = check.run_two_engine_driver([], [{"index": 0, "text": text}])
            assert translated.atoms.get(0) == measured, f"T-DOM of {atom!r}: {text!r}"
        else:
            assert verdict.kind == "reported", verdict
            assert verdict.reason == f"{SEMANTIC_PREFIX}{engine}:{construct}", verdict
    for item in found:
        verdict = check.classify(item.pattern, item.consumer, domain)
        if verdict.decisions:
            assert verdict.kind == "reported", _row_message(item, verdict)


# ---------------------------------------------------------------------------------------------
# Universal and witnessless rewrites
# ---------------------------------------------------------------------------------------------


def _class_body(pattern: str) -> tool.Node:
    """The class node inside the lookahead of a pattern ``a(?!<class>)``."""
    stack = [tool.lex(pattern)]
    while stack:
        node = stack.pop()
        if node.kind == "class":
            return node
        stack.extend(reversed(node.children))
    raise AssertionError(f"no class node in {pattern!r}")


def test_universal_and_witnessless_rewrites_are_classified() -> None:
    universal = _expect("a*(?=)", "BOOL", "rewrite")
    assert universal.python_text == "a*" and universal.universal is True, universal
    assert check.matches_everything("a*", "BOOL")
    for text in ("a+", "^a*", "a*$", "a*\\b"):
        assert not check.matches_everything(text, "BOOL"), text
    assert not check.matches_everything("a*", "FULLMATCH")
    with mock.patch.object(check, "derive_probes", side_effect=lambda o, p, c, limit=2000: ["ac"]):
        _expect("a(?=c)", "BOOL", "reported", "no-distinguishing-witness")
    _expect("(?:a(?=b)){2}", "BOOL", "reported", "lookaround-under-repetition")
    fold = _stable_table(
        {
            ("re", "(?i:q)"): ((81, 81), (113, 113)),
            ("re", "(?i:a)"): ((65, 65), (97, 97)),
            ("re", "(?i:A)"): ((65, 65), (97, 97)),
        }
    )
    _expect("(?i)q(?=.*a)(?=.*A)", "BOOL", "reported", "lookaround-overlapping-literals", fold)
    _expect("(?=.*ab)(?=.*bc)", "BOOL", "reported", "lookaround-overlapping-literals")
    r5 = _expect("q(?=.*a)(?=.*A)", "BOOL", "rewrite")
    assert "R5" in r5.families, r5
    for pattern in ("q(?=.*\n)(?=.*a)", "q(?=.*a\nb)(?=.*c)", "(?s)q(?=.*\n)(?=.*a)"):
        _expect(pattern, "BOOL", "reported", "lookaround-newline-body")
    dotall = _expect("(?s)q(?=.*x)(?=.*y)", "BOOL", "rewrite")
    assert "R5" in dotall.families, dotall
    witness = check.check_rewrite("q(?=.*\n)(?=.*a)", "q(?:.*\n.*a|.*a.*\n)", "BOOL", ["q\na"])
    assert witness == "q\na", witness
    _expect("a(?![\nb])", "BOOL", "reported", "lookaround-newline-body")
    _expect("a(?!b\nc)", "BOOL", "reported", "lookaround-newline-body")
    for pattern in (
        "a(?![\\nb])",
        "a(?![\\x0ab])",
        "a(?![\\t-\\r])",
        "a(?![^b])",
        "a(?![\\W])",
        "a(?![\\D])",
        "a(?![\\s])",
    ):
        assert tool.class_matches_newline(_class_body(pattern)) is True, pattern
    for pattern in (
        "a(?![bc])",
        "a(?![^\\nb])",
        "a(?![\\w])",
        "a(?![\\d])",
        "a(?![\\S])",
        "a(?![^\\W])",
        "a(?![^\\s])",
    ):
        assert tool.class_matches_newline(_class_body(pattern)) is False, pattern
    for pattern in ("a(?![\\x0ab])", "a(?![\\t-\\r])", "a(?![^b])"):
        _expect(pattern, "BOOL", "reported", "lookaround-newline-body")
    # A negated body that cannot match a newline is outside every family: the fallback report.
    fallback = _expect("a(?![^\\nb])", "BOOL", "reported", "lookaround-unsupported-shape")
    assert fallback.python_text is None and fallback.rust_text is None, fallback
    case_fold = _stable_table(
        {("re", "(?i:a)"): ((65, 65), (97, 97)), ("re", "(?i:A)"): ((65, 65), (97, 97))}
    )
    assert check.literals_independent("a", "A", re.I, case_fold) is False
    assert check.literals_independent("a", "A", 0, EMPTY) is True
    assert check.literals_independent("ab", "bc", 0, EMPTY) is False
    assert check.literals_independent("ab", "b", 0, EMPTY) is False


# ---------------------------------------------------------------------------------------------
# Consumer observables: flags, substitution and boundaries
# ---------------------------------------------------------------------------------------------

DIGIT_13 = ((48, 57), (0x660, 0x669))
DIGIT_14 = ((48, 57), (0x660, 0x66A))
DIGIT_OTHER = ((48, 57), (0x6F0, 0x6F9))


def _digit_table(python: Sequence[tuple[int, int]], rust: Sequence[tuple[int, int]]) -> Any:
    table = _stable_table({("re", "\\d"): python, ("regex", "\\d"): rust}, {"\\d": rust})
    digests = dict(table.python_digest)
    digests[("re", "3.13", "\\d")] = check.membership_digest(DIGIT_13)
    digests[("re", "3.14", "\\d")] = check.membership_digest(DIGIT_14)
    return _replace(table, python_digest=digests)


def _check_flag_constants() -> None:
    assert set(tool.CONSUMER_FLAGS) == set(tool.CONSUMER_CLASSES)
    assert set(tool.CONSUMER_MODES) == set(tool.CONSUMER_CLASSES)
    for consumer in tool.CONSUMER_CLASSES:
        expected = {"SPAN-ALLOWLIST": re.IGNORECASE, "SEARCH-PKG-M": regex.MULTILINE}
        assert check.consumer_flags(consumer) == expected.get(consumer, 0), consumer
    compiled = compile_safe_regex("q")
    pattern_object = compiled._compiled
    assert isinstance(pattern_object, regex.Pattern) and pattern_object.flags & regex.MULTILINE
    allowlisted = ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS[1]
    upper = "It is NOT slow, BUT fast."
    assert first_response_match(upper, [allowlisted], regex=True) is not None
    template = "{{ t | regex_replace(p, r) }}"
    rendered = TemplateEngine().render(
        template, {"t": "id syn-42 syn-7", "p": "syn-(\\d+)", "r": "n"}
    )
    assert rendered == "id n n", rendered


def _check_sub_iteration() -> None:
    for pattern, subject in (
        ("syn-([0-9]+)", "id syn-42 syn-7"),
        ("x*", "abxd"),
        ("a|", "baac"),
        ("(a)?b", "b ab"),
    ):
        calls: list[tuple[tuple[int, int], tuple[tuple[int, int] | None, ...]]] = []

        def record(match: re.Match[str], calls: list[Any] = calls) -> str:
            calls.append((match.span(), check.group_spans(match)))
            return tool.SUB_REPLACEMENT

        re.sub(pattern, record, subject)
        found = [(m.span(), check.group_spans(m)) for m in re.finditer(pattern, subject)]
        assert calls == found, f"{pattern!r} on {subject!r}"
        if pattern == "(a)?b":
            assert calls[0][1] == (None,), calls


def _verified(rows: Sequence[tuple[tool.FoundRegex, check.Verdict]]) -> list[check.Verdict]:
    after = check.verify(rows)
    assert len(after) == len(rows), f"verify returned {len(after)} rows for {len(rows)}"
    return [verdict for _, verdict in after]


def _found(pattern: str, consumer: str) -> tool.FoundRegex:
    return tool.FoundRegex("synthetic.yaml", "synthetic", pattern, consumer, "key")


def _check_sub_verdicts() -> None:
    sub = _expect("q-([0-9]+)", "SUB", "as_written")
    probes = check.derive_probes("q-([0-9]+)", "q-([0-9]+)", "SUB")
    entry = check.work_entry(0, "q-([0-9]+)", "SUB", probes)
    assert entry.get("mode") == "sub" and entry.get("replacement") == tool.SUB_REPLACEMENT
    multi = [p for p in probes if len(_sub_matches("SUB", "q-([0-9]+)", p)) >= 2]
    assert multi, f"no two-match probe in {probes}"
    results = check.run_two_engine_driver([check.work_entry(0, "q-([0-9]+)", "SUB", multi)], [])
    assert len(results.patterns) == 1, "driver results"
    observed = check.observable("SUB", "q-([0-9]+)", multi[0])
    assert check.compare_observable("SUB", observed, _probe(results, 0)) is None
    assert _verified([(_found("q-([0-9]+)", "SUB"), sub)])[0].verified is True
    _expect("\\Aq-([0-9]+)", "SUB", "as_written")
    for pattern in ("x*", "a|"):
        _expect(pattern, "SUB", "reported", "sub-empty-match-iteration")
        assert tool.min_width(tool.lex(pattern)) == 0
    real = check.derive_probes

    def single_probes(original: str, text: str, consumer: str, limit: int = 2000) -> list[str]:
        return [
            p
            for p in real(original, text, consumer, limit)
            if len(_sub_matches(consumer, original, p)) < 2
        ]

    with mock.patch.object(check, "derive_probes", side_effect=single_probes):
        _expect("q-([0-9]+)", "SUB", "reported", "no-multi-match-witness")
    real_entry = check.work_entry

    def search_entry(
        index: int, text: str, consumer: str, probes: Sequence[str]
    ) -> dict[str, object]:
        entry = real_entry(index, text, consumer, probes)
        entry["mode"] = "search"
        entry.pop("replacement", None)
        return entry

    with mock.patch.object(check, "work_entry", side_effect=search_entry):
        mutated = _verified([(_found("q-([0-9]+)", "SUB"), sub)])[0]
    assert mutated.kind == "fail" and str(mutated.reason).startswith("two-engine-mismatch")


def _span_table(*patterns: str) -> check.DomainTable:
    """A stable table of the case-folded atoms SPAN-ALLOWLIST reads in ``patterns``."""
    return _stable_table(
        {
            ("re", atom): check.python_membership(atom, "re")
            for pattern in patterns
            for _, atom in tool.engine_atoms(tool.lex(pattern), "SPAN-ALLOWLIST")
        }
    )


def _check_span_verdicts() -> None:
    table = _span_table("not slow", "[0-9]+")
    span = _expect("not slow", "SPAN-ALLOWLIST", "as_written", table=table)
    probes = check.derive_probes("not slow", "not slow", "SPAN-ALLOWLIST")
    assert "NOT SLOW" in probes, probes
    entry = check.work_entry(0, "not slow", "SPAN-ALLOWLIST", probes)
    assert entry.get("flags") == {"case_insensitive": True, "multi_line": False}, entry
    assert check.observable("SPAN-ALLOWLIST", "not slow", "NOT SLOW") != check.observable(
        "SPAN-ALLOWLIST", "not slow", "NOT SLOW", without="case_insensitive"
    )
    real_entry = check.work_entry

    def sensitive_entry(
        index: int, text: str, consumer: str, probes: Sequence[str]
    ) -> dict[str, object]:
        entry = real_entry(index, text, consumer, probes)
        entry["flags"] = {"case_insensitive": False, "multi_line": False}
        return entry

    with mock.patch.object(check, "work_entry", side_effect=sensitive_entry):
        mutated = _verified([(_found("not slow", "SPAN-ALLOWLIST"), span)])[0]
    assert mutated.kind == "fail" and str(mutated.reason).startswith("two-engine-mismatch")
    real = check.derive_probes

    def no_swap(original: str, text: str, consumer: str, limit: int = 2000) -> list[str]:
        return [p for p in real(original, text, consumer, limit) if p == p.lower()]

    with mock.patch.object(check, "derive_probes", side_effect=no_swap):
        _expect("not slow", "SPAN-ALLOWLIST", "reported", "no-flag-witness", table=table)
        _expect("[0-9]+", "SPAN-ALLOWLIST", "as_written", table=table)
    assert tool.flag_inert(tool.lex("[0-9]+"), "SPAN-ALLOWLIST", "case_insensitive")
    assert not tool.flag_inert(tool.lex("not slow"), "SPAN-ALLOWLIST", "case_insensitive")
    assert check.python_membership("(?i:[0-9])", "re") == check.python_membership("[0-9]", "re")
    assert check.python_membership("(?i:n)", "re") != check.python_membership("n", "re")


def _check_pkg_and_external_flags() -> None:
    probes = check.derive_probes("^b", "^b", "SEARCH-PKG-M")
    assert "x\nb" in probes, probes
    entry = check.work_entry(0, "^b", "SEARCH-PKG-M", probes)
    assert entry.get("flags") == {"case_insensitive": False, "multi_line": True}, entry
    _expect("^b", "SEARCH-PKG-M", "as_written")
    span_atoms = [atom for _, atom in tool.engine_atoms(tool.lex("k"), "SPAN-ALLOWLIST")]
    assert span_atoms == ["(?i:k)"], span_atoms
    assert tool.engine_atoms(tool.lex("k"), "BOOL") == []
    wide = tuple((point, point) for point in range(0x100, 0x100 + 2 * 70, 2))
    table = _stable_table({("re", "(?i:k)"): ((75, 75), (107, 107))}, {"(?i:k)": wide})
    _expect("k", "SPAN-ALLOWLIST", "reported", table=table)
    _expect("k", "BOOL", "as_written", table=table)


def _boundary_table(constructs: Mapping[str, Any], word_rust: Any = None) -> check.DomainTable:
    word = ((48, 57), (65, 90), (95, 95), (97, 122))
    python_boundary: dict[tuple[str, str, str], dict[str, tuple[int, ...]]] = {}
    rust_boundary: dict[str, dict[str, tuple[int, ...]]] = {}
    for row in constructs["boundary"]:
        for subject, engines in row["offsets"].items():
            for engine in ("re", "regex"):
                value = engines[engine]
                for minor in check.SUPPORTED_PYTHON_MINORS:
                    picked = value[minor] if isinstance(value, dict) else value
                    key = (engine, minor, row["atom"])
                    python_boundary.setdefault(key, {})[subject] = tuple(picked)
            rust_boundary.setdefault(row["atom"], {})[subject] = tuple(engines["rust"])
    return _stable_table(
        {("re", "\\w"): word, ("regex", "\\w"): word},
        {"\\w": word_rust or word},
        python_boundary=python_boundary,
        rust_boundary=rust_boundary,
    )


def _check_boundary_verdicts(constructs: Mapping[str, Any]) -> None:
    table = _boundary_table(constructs)
    restricted = _expect("\\Bq", "BOOL", "as_written", table=table)
    assert restricted.boundaries and restricted.boundaries[0].get("empty") == (
        "restricted:nonempty-alternative"
    ), restricted
    empty_id = f"{SEMANTIC_PREFIX}re:non-word-boundary-empty-subject"
    for pattern in ("x*\\B", "a|\\B"):
        _expect(pattern, "BOOL", "reported", empty_id, table=table)
    _expect("(?:\\B|a)b", "BOOL", "as_written", table=table)
    pkg = _verdict("x*\\B", "SEARCH-PKG-M", table)
    assert pkg.boundaries and pkg.boundaries[0].get("empty") == "equal", pkg
    word = _expect("x*\\b", "BOOL", "as_written", table=table)
    assert all(word.boundaries[0].get(g) == "equal" for g in tool.BOUNDARY_SUBJECTS), word
    accepted_id = "re:non-word-boundary-empty-subject"
    with mock.patch.object(tool, "ACCEPTED_DIVERGENCES", frozenset({accepted_id})):
        accepted = _expect("x*\\B", "BOOL", "as_written", table=table)
        assert accepted_id in accepted.atoms, accepted
    with mock.patch.object(tool, "ACCEPTED_DIVERGENCES", frozenset({"re:non-word-boundary"})):
        _expect("x*\\B", "BOOL", "reported", table=table)
    edge = {key: dict(value) for key, value in table.python_boundary.items()}
    for minor in check.SUPPORTED_PYTHON_MINORS:
        edge[("re", minor, "\\B")]["ab"] = ()
    _expect(
        "\\Bq",
        "BOOL",
        "reported",
        f"{SEMANTIC_PREFIX}re:non-word-boundary",
        table=_replace(table, python_boundary=edge),
    )
    other = "3.14" if RUNNING_MINOR == "3.13" else "3.13"
    lacking = {key: value for key, value in table.python_boundary.items() if key[1] != other}
    _expect(
        "x*\\b",
        "BOOL",
        "reported",
        f"{SEMANTIC_PREFIX}re:word-boundary",
        table=_replace(table, python_boundary=lacking),
    )
    differing = _boundary_table(constructs, word_rust=((48, 57), (65, 90), (97, 122)))
    general = _expect(
        "\\Bq", "BOOL", "reported", f"{SEMANTIC_PREFIX}re:non-word-boundary", table=differing
    )
    assert general.boundaries[0].get("membership") == "decision", general
    probes = check.derive_probes("\\Bq", "\\Bq", "BOOL")
    assert probes and probes[0] == "", probes
    subjects = [s for group in tool.BOUNDARY_SUBJECTS.values() for s in group]
    assert set(subjects) | {"!q", "q!", "aq", "qa"} <= set(probes), probes
    for consumer in tool.CONSUMER_CLASSES:
        assert check.derive_probes("q", "q", consumer)[:1] == [""], consumer


def _check_membership_verdicts() -> None:
    version_reason = f"{SEMANTIC_PREFIX}re:digit-shorthand-version-difference"
    as_313 = _digit_table(DIGIT_13, DIGIT_13)
    as_314 = _digit_table(DIGIT_14, DIGIT_14)
    left = _expect("q\\d", "BOOL", "reported", version_reason, as_313)
    right = _expect("q\\d", "BOOL", "reported", version_reason, as_314)
    assert (left.kind, left.reason, left.families) == (right.kind, right.reason, right.families)
    assert "T-DOM" not in left.families
    for python in (DIGIT_13, DIGIT_14):
        verdict = _expect(
            "q\\d", "BOOL", "reported", version_reason, _digit_table(python, DIGIT_OTHER)
        )
        assert verdict.kind != "rewrite"
    equal = _stable_table({("re", "\\d"): DIGIT_13})
    _expect("q\\d", "BOOL", "as_written", table=equal)
    differs = _stable_table({("re", "\\d"): DIGIT_13}, {"\\d": DIGIT_14})
    verdict = _expect("q\\d", "BOOL", "rewrite", table=differs)
    assert "T-DOM" in verdict.families, verdict
    missing = {k: v for k, v in equal.python_digest.items() if k != ("re", "3.14", "\\d")}
    _expect("q\\d", "BOOL", "reported", version_reason, _replace(equal, python_digest=missing))
    pkg = _expect("q\\d", "SEARCH-PKG-M", "as_written", table=as_313)
    assert not any(d.endswith("-version-difference") for d in pkg.decisions), pkg
    class_table = _digit_table(DIGIT_13, DIGIT_13)
    class_python = dict(class_table.python)
    class_python[("re", "[\\d_]")] = (*DIGIT_13[:1], (95, 95), *DIGIT_13[1:])
    class_python[("re", "[a-c]")] = ((97, 99),)
    class_python[("re", "_")] = ((95, 95),)
    class_rust = dict(class_table.rust)
    class_rust["[\\d_]"] = class_python[("re", "[\\d_]")]
    class_rust["[a-c]"] = ((97, 99),)
    classes = _replace(class_table, python=class_python, rust=class_rust)
    unstable_class = _expect("[\\d_]", "BOOL", "reported", table=classes)
    assert any(d.endswith("-version-difference") for d in unstable_class.decisions)
    _expect("[a-c]", "BOOL", "as_written", table=classes)
    broken = _stable_table(
        {("re", "\\d"): DIGIT_13, ("re", "_"): ((95, 95),), ("re", "[\\d_]"): ((95, 95),)}
    )
    assert broken.version_stable("BOOL", tool.lex("[\\d_]")) is False
    _expect("[\\d_]", "BOOL", "reported", table=broken)
    outside = _stable_table({("re", "(?i:q)"): ((81, 81), (113, 113))})
    verdict = _expect("(?i)q\u00b7", "BOOL", "reported", table=outside)
    assert any(d.endswith("-version-difference") for d in verdict.decisions), verdict
    word = ((48, 57), (65, 90), (95, 95), (97, 122))
    word_table = _split_digest(_stable_table({("re", "\\w"): word}), "re", "\\w")
    for pattern, construct in (("\\bq", "word-boundary"), ("\\Bq", "non-word-boundary")):
        verdict = _expect(
            pattern,
            "BOOL",
            "reported",
            f"{SEMANTIC_PREFIX}re:{construct}-version-difference",
            word_table,
        )
        assert verdict.boundaries[0].get("membership") == "version-difference", verdict
    _expect(
        "x*\\B",
        "BOOL",
        "reported",
        f"{SEMANTIC_PREFIX}re:non-word-boundary-version-difference",
        word_table,
    )
    _expect("x*\\B", "SUB", "reported", "sub-empty-match-iteration", word_table)
    for accepted, kind, reason in (
        ({"re:digit-shorthand-version-difference"}, "reported", "re:digit-shorthand"),
        ({"re:digit-shorthand"}, "reported", "re:digit-shorthand-version-difference"),
        ({"re:digit-shorthand", "re:digit-shorthand-version-difference"}, "as_written", None),
    ):
        with mock.patch.object(tool, "ACCEPTED_DIVERGENCES", frozenset(accepted)):
            for table in (as_313, as_314):
                verdict = _expect(
                    "q\\d",
                    "BOOL",
                    kind,
                    None if reason is None else SEMANTIC_PREFIX + reason,
                    table,
                )
                if kind == "as_written":
                    assert set(accepted) <= set(verdict.atoms), verdict
    version_id = "re:digit-shorthand-version-difference"
    for pattern, consumer, reason in (
        ("\\d(?!bc)d", "BOOL", "lookaround-continuation"),
        ("x*\\d*", "SUB", "sub-empty-match-iteration"),
        ("\\d", "INERT-EXEMPTION", "inert-no-command-pattern"),
    ):
        sides = [_expect(pattern, consumer, "reported", reason, t) for t in (as_313, as_314)]
        assert all(version_id in v.decisions for v in sides), sides
        assert sides[0].decisions == sides[1].decisions
        _expect(pattern, consumer, "reported", reason, equal)


def _check_verify_against_a_wrong_driver() -> None:
    rows = [
        (_found("q+", "BOOL"), _expect("q+", "BOOL", "as_written")),
        (_found("a(?=b)", "BOOL"), _expect("a(?=b)", "BOOL", "rewrite")),
        (
            _found("a(?!bc)d", "BOOL"),
            _expect("a(?!bc)d", "BOOL", "reported", "lookaround-continuation"),
        ),
    ]
    with mock.patch.object(check, "derive_probes", side_effect=lambda o, p, c, limit=2000: ["ac"]):
        proposal = _expect("a(?=c)", "BOOL", "reported", "no-distinguishing-witness")
    assert proposal.python_text == "a(?:c)", proposal
    rows.append((_found("a(?=c)", "BOOL"), proposal))

    def agreeing(entries: Sequence[Mapping[str, Any]]) -> dict[int, object]:
        by_text = {e["text"]: e for e in entries}
        results: dict[int, object] = {}
        for index, (item, verdict) in enumerate(rows):
            entry = by_text.get(str(verdict.rust_text), {"probes": []})
            results[index] = tuple(
                _driver_shape(check.observable("BOOL", item.pattern, p)) for p in entry["probes"]
            )
        return results

    def inverted(entries: Sequence[Mapping[str, Any]]) -> dict[int, object]:
        flipped: dict[int, object] = {}
        for index, result in agreeing(entries).items():
            assert isinstance(result, tuple)
            flipped[index] = tuple((not value[0], *value[1:]) for value in result)
        return flipped

    cases: list[tuple[str, Callable[[Sequence[Mapping[str, Any]]], dict[int, object]]]] = [
        ("agreeing", agreeing),
        ("error", lambda entries: {index: {"error": "synthetic"} for index in range(len(rows))}),
        ("inverted", inverted),
        ("empty", lambda entries: {}),
    ]
    for label, answer in cases:
        sent: list[Mapping[str, Any]] = []

        def driver(
            patterns: Sequence[Mapping[str, Any]],
            atoms: Any,
            boundaries: Any = (),
            sent: list[Mapping[str, Any]] = sent,
            answer: Callable[[Sequence[Mapping[str, Any]]], dict[int, object]] = answer,
        ) -> Any:
            sent.extend(patterns)
            return check.DriverResults(patterns=answer(patterns))

        with mock.patch.object(check, "run_two_engine_driver", side_effect=driver):
            after = _verified(rows)
        assert sorted(str(e["text"]) for e in sent) == ["a(?:b)", "q+"], f"{label}: {sent}"
        assert after[2] == rows[2][1] and after[3] == rows[3][1], f"{label}: report rows changed"
        if label == "agreeing":
            assert after[0].verified and after[1].verified, f"{label}: {after}"
        elif label == "empty":
            for verdict, (_, before) in zip(after[:2], rows[:2], strict=True):
                assert verdict.kind == "reported" and verdict.reason == "unverified-proposal"
                assert verdict.verified is False and verdict.rust_text == before.rust_text
        else:
            for verdict in after[:2]:
                assert verdict.kind == "fail", f"{label}: {verdict}"
                assert str(verdict.reason).startswith("two-engine-mismatch"), verdict


def _driver_shape(value: object) -> tuple[object, ...]:
    """A Python BOOL observable written as a driver probe result."""
    return (bool(value), None, ())


def test_consumer_observables_carry_flags_and_substitution() -> None:
    constructs = _load(CONSTRUCTS_FIXTURE)
    _check_flag_constants()
    _check_sub_iteration()
    _check_sub_verdicts()
    _check_span_verdicts()
    _check_pkg_and_external_flags()
    _check_boundary_verdicts(constructs)
    _check_membership_verdicts()
    _, domain, fixture = _live()
    assert domain.boundary_holds("BOOL", "\\B", "empty") is False
    row = next(r for r in fixture["boundary"] if r["atom"] == "\\B")
    measured = domain.python_boundary.get(("re", RUNNING_MINOR, "\\B"), {})
    assert list(measured.get("", ())) == _version(row["offsets"][""]["re"])
    assert domain.version_stable("BOOL", tool.lex("\\d")) is False
    assert domain.version_stable("SEARCH-PKG-M", tool.lex("\\d")) is True
    _check_verify_against_a_wrong_driver()


# ---------------------------------------------------------------------------------------------
# R5 independence under ignore-case
# ---------------------------------------------------------------------------------------------

R5_PATTERN = "(?i)q(?=.*a)(?=.*b)"
R5_Q = ((81, 81), (113, 113))
SEP = {"(?i:a)": ((65, 65), (97, 97)), "(?i:b)": ((66, 66), (98, 98))}
MIX = {"(?i:a)": ((65, 66), (97, 98)), "(?i:b)": ((65, 66), (97, 98))}
R5_BASIS = ("(?i:\\x71)", "(?i:\\x61)", "(?i:\\x62)")


class _RecordingMapping(Mapping[tuple[str, str], tuple[tuple[int, int], ...]]):
    def __init__(self, data: Mapping[tuple[str, str], tuple[tuple[int, int], ...]]) -> None:
        self._data = dict(data)
        self.read: list[tuple[str, str]] = []

    def __getitem__(self, key: tuple[str, str]) -> tuple[tuple[int, int], ...]:
        self.read.append(key)
        return self._data[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def _r5_python(sets: Mapping[str, tuple[tuple[int, int], ...]]) -> dict[tuple[str, str], Any]:
    python: dict[tuple[str, str], RangeSet] = {("re", "(?i:q)"): R5_Q}
    python.update({("re", atom): value for atom, value in sets.items()})
    return python


def _r5_table(sets: Mapping[str, Any], digests: Mapping[tuple[str, str, str], str]) -> Any:
    python = _r5_python(sets)
    return check.DomainTable(
        python=python,
        rust={atom: value for (_, atom), value in python.items()},
        python_digest=dict(digests),
    )


def _differ_digests(basis: Sequence[str]) -> dict[tuple[str, str, str], str]:
    return {
        ("re", minor, atom): f"synthetic-{minor}-{atom}"
        for atom in basis
        for minor in check.SUPPORTED_PYTHON_MINORS
    }


def _missing_digests(basis: Sequence[str], minor: str) -> dict[tuple[str, str, str], str]:
    return {
        ("re", other, atom): f"synthetic-{atom}"
        for atom in basis
        for other in check.SUPPORTED_PYTHON_MINORS
        if other != minor
    }


def _assert_fold_report(verdict: check.Verdict) -> None:
    assert verdict.kind == "reported", verdict
    assert verdict.reason == "lookaround-fold-not-version-stable", verdict
    assert verdict.families == () and verdict.python_text is None and verdict.rust_text is None
    assert any(d.endswith("-version-difference") for d in verdict.decisions), verdict


def test_r5_fold_independence_reads_version_stable_memberships_only() -> None:
    unstable = [_differ_digests(R5_BASIS)] + [
        _missing_digests(R5_BASIS, minor) for minor in check.SUPPORTED_PYTHON_MINORS
    ]
    for digests in unstable:
        left = _verdict(R5_PATTERN, "BOOL", _r5_table(SEP, digests))
        right = _verdict(R5_PATTERN, "BOOL", _r5_table(MIX, digests))
        _assert_fold_report(left)
        assert left == right, f"{left} != {right}"
    p2 = "(?i)q(?=.*\u1c89)(?=.*\u1c8a)"
    l13 = {"(?i:\u1c89)": ((0x1C89, 0x1C89),), "(?i:\u1c8a)": ((0x1C8A, 0x1C8A),)}
    l14 = {"(?i:\u1c89)": ((0x1C89, 0x1C8A),), "(?i:\u1c8a)": ((0x1C89, 0x1C8A),)}
    q_only = {("re", minor, "(?i:\\x71)"): "synthetic-q" for minor in check.SUPPORTED_PYTHON_MINORS}
    first = _verdict(p2, "BOOL", _r5_table(l13, q_only))
    _assert_fold_report(first)
    assert _verdict(p2, "BOOL", _r5_table(l14, q_only)) == first
    measured = {atom: check.python_membership(atom, "re") for atom in l13}
    assert measured in (l13, l14), f"measured fold sets {measured}"
    assert _verdict(p2, "BOOL", _r5_table(measured, q_only)) == first
    stable_digests = {
        ("re", minor, atom): f"synthetic-{atom}"
        for atom in R5_BASIS
        for minor in check.SUPPORTED_PYTHON_MINORS
    }
    rewrite = _expect(R5_PATTERN, "BOOL", "rewrite", table=_r5_table(SEP, stable_digests))
    assert "R5" in rewrite.families, rewrite
    assert rewrite.python_text == "(?i)q(?:.*a.*b|.*b.*a)", rewrite
    _expect(
        R5_PATTERN,
        "BOOL",
        "reported",
        "lookaround-overlapping-literals",
        _r5_table(MIX, stable_digests),
    )
    extra = ("(?i:\\x63)",)
    order_digests = _differ_digests(R5_BASIS + extra)
    with_c = {**SEP, "(?i:c)": ((67, 67), (99, 99))}
    _expect(
        "(?i)q(?=.*a\nb)(?=.*c)",
        "BOOL",
        "reported",
        "lookaround-newline-body",
        _r5_table(with_c, order_digests),
    )
    for sets in (with_c, {**MIX, "(?i:c)": ((67, 67), (99, 99))}):
        _expect(
            "(?i)(?=.*ab)(?=.*bc)",
            "BOOL",
            "reported",
            "lookaround-fold-not-version-stable",
            _r5_table(sets, order_digests),
        )
    plain = _expect("q(?=.*a)(?=.*b)", "BOOL", "rewrite")
    assert "R5" in plain.families, plain
    for body, basis in (("\\W", "\\W"), ("\\D", "\\D"), ("\\s", "\\s")):
        pattern = f"a(?![{body}])"
        digests = {
            ("re", "3.13", basis): "synthetic-313",
            ("re", "3.14", basis): "synthetic-314",
        }
        verdicts = []
        for fill in (NO_SCALARS, ALL_SCALARS):
            python = {("re", basis): fill, ("re", f"[{body}]"): fill}
            table = check.DomainTable(
                python=python, rust={basis: ALL_SCALARS}, python_digest=digests
            )
            verdicts.append(_verdict(pattern, "BOOL", table))
        assert verdicts[0] == verdicts[1], f"{pattern}: {verdicts}"
        assert verdicts[0].kind == "reported", verdicts[0]
        assert verdicts[0].reason == "lookaround-newline-body", verdicts[0]
        assert "R2-" not in verdicts[0].families, verdicts[0]
        assert any(d.endswith("-version-difference") for d in verdicts[0].decisions)
        assert tool.class_matches_newline(_class_body(pattern)) is True, pattern
    for sets in (SEP, MIX):
        for digests in unstable:
            table = _r5_table(sets, digests)
            recording = _RecordingMapping(table.python)
            watched = _replace(table, python=recording)
            assert check.literals_independent("a", "b", re.I, watched) is None
            assert recording.read == [], f"an unstable fold was read: {recording.read}"
    assert check.literals_independent("a", "b", re.I, _r5_table(SEP, stable_digests)) is True
    assert check.literals_independent("a", "b", re.I, _r5_table(MIX, stable_digests)) is False
    for table in (_r5_table(l13, q_only), _r5_table(l14, q_only), _r5_table(SEP, stable_digests)):
        assert check.literals_independent("\u1c89", "\u1c8a", re.I, table) is None
    assert check.literals_independent("a", "b", 0, EMPTY) is True
