"""Regex audit: the classifier, the shape families, probes and the two-engine run.

``classify`` gives each found pattern a verdict for its consumer class: exact as written, an exact
rewrite, a report row, or a failure. ``verify`` then sends every exact text through the Rust
driver in crates/gport/tests/regex_audit.rs and compares each probe with the Python observable of
the original pattern.

``classify`` and ``verify`` call ``derive_probes``, ``work_entry`` and ``run_two_engine_driver``
through this module's namespace at call time, so a test can patch each of them. The consumer
drivers and the record functions live in regex_audit_record.py and are re-exported here.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import regex

from gobby.workflows.assistant_response_matching import ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS
from tests.contracts import regex_audit_tool as tool
from tests.contracts.regex_audit_record import (
    drive_consumer,
    record_consumer_cases,
    record_regex_audit,
)
from tests.contracts.regex_audit_tool import FoundRegex, Node

SUPPORTED_PYTHON_MINORS = ("3.13", "3.14")
RUNNING_MINOR = f"{sys.version_info[0]}.{sys.version_info[1]}"
CONSTRUCTS_FIXTURE = Path(__file__).resolve().parent / "regex_audit" / "constructs.json"
ALL_SCALARS = ((0x0, 0xD7FF), (0xE000, 0x10FFFF))
SEMANTIC_PREFIX = "semantic-decision:"
Ranges = tuple[tuple[int, int], ...]


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
        """The consumer engine's measured set of ``atom``; ValueError when the table lacks it."""
        key = (tool.CONSUMER_ENGINES[consumer], atom)
        if key not in self.python:
            raise ValueError(f"the table holds no {key[0]} membership for {atom!r}")
        return tuple((int(low), int(high)) for low, high in self.python[key])

    def rust_for(self, atom: str) -> tuple[tuple[int, int], ...]:
        if atom not in self.rust:
            raise ValueError(f"the table holds no Rust membership for {atom!r}")
        return tuple((int(low), int(high)) for low, high in self.rust[atom])

    def basis_stable(self, consumer: str, members: Sequence[str] | None) -> bool:
        if members is None:
            return False
        engine = tool.CONSUMER_ENGINES[consumer]
        for basis in members:
            values = [
                self.python_digest.get((engine, minor, basis)) for minor in SUPPORTED_PYTHON_MINORS
            ]
            if any(value is None for value in values) or len(set(values)) != 1:
                return False
        return True

    def version_stable(self, consumer: str, atom: Node) -> bool:
        """The atom's set is the same on every supported minor (digest rows and composition)."""
        if not self.basis_stable(consumer, tool.basis_members(atom, consumer)):
            return False
        items = cast(Sequence[str], atom.detail.get("items") or ()) if atom.kind == "class" else ()
        if not items:
            return True
        engine = tool.CONSUMER_ENGINES[consumer]
        parts = []
        for item in items:
            key = (engine, str(item))
            parts.append(
                self.python[key] if key in self.python else python_membership(str(item), engine)
            )
        composed = _union(parts)
        if atom.detail.get("negated"):
            composed = _complement(composed)
        text = str(atom.detail.get("source") or "")
        try:
            measured = self.python_for(consumer, text)
        except ValueError:
            return False
        return composed == _union([measured])

    def boundary_holds(self, consumer: str, atom: str, group: str) -> bool:
        """Python offsets equal Rust's on every subject of ``group``, on every supported minor."""
        engine = tool.CONSUMER_ENGINES[consumer]
        rust = self.rust_boundary.get(atom)
        if rust is None:
            return False
        for minor in SUPPORTED_PYTHON_MINORS:
            python = self.python_boundary.get((engine, minor, atom))
            if python is None:
                return False
            for subject in tool.BOUNDARY_SUBJECTS[group]:
                if subject not in python or subject not in rust:
                    return False
                if tuple(python[subject]) != tuple(rust[subject]):
                    return False
        return True


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


# ---------------------------------------------------------------------------------------------
# Sets, digests and fixture values
# ---------------------------------------------------------------------------------------------


def _points(ranges: Iterable[Sequence[int]]) -> set[int]:
    return {point for low, high in ranges for point in range(low, high + 1)}


def _merge(points: Iterable[int]) -> Ranges:
    ranges: list[tuple[int, int]] = []
    for point in sorted(set(points)):
        if ranges and ranges[-1][1] + 1 == point:
            ranges[-1] = (ranges[-1][0], point)
        else:
            ranges.append((point, point))
    return tuple(ranges)


def _combine(left: Iterable[Sequence[int]], right: Iterable[Sequence[int]], op: str) -> Ranges:
    """A set operation (``or``, ``xor``, ``sub``) on two range sets, without expanding points."""
    edges: dict[int, list[int]] = {}
    for side, ranges in ((0, left), (1, right)):
        for low, high in ranges:
            edges.setdefault(low, [0, 0])[side] += 1
            edges.setdefault(high + 1, [0, 0])[side] -= 1
    operations: dict[str, Callable[[bool, bool], bool]] = {
        "or": lambda a, b: a or b,
        "xor": lambda a, b: a != b,
        "sub": lambda a, b: a and not b,
    }
    keep = operations[op]
    result: list[tuple[int, int]] = []
    counts = [0, 0]
    start: int | None = None
    for point in sorted(edges):
        counts[0] += edges[point][0]
        counts[1] += edges[point][1]
        inside = keep(counts[0] > 0, counts[1] > 0)
        if inside and start is None:
            start = point
        elif not inside and start is not None:
            result.append((start, point - 1))
            start = None
    return tuple(result)


def _union(sets: Iterable[Iterable[Sequence[int]]]) -> Ranges:
    total: Ranges = ()
    for ranges in sets:
        total = _combine(total, ranges, "or")
    return total


def _complement(ranges: Iterable[Sequence[int]]) -> Ranges:
    return _combine(ALL_SCALARS, ranges, "sub")


def by_version(value: object, minor: str | None = None) -> object:
    """A fixture value for one minor: a mapping is keyed by minor, anything else is plain."""
    if isinstance(value, Mapping):
        return value.get(minor or RUNNING_MINOR)
    return value


@functools.cache
def construct_rows() -> tuple[Mapping[str, object], ...]:
    return tuple(json.loads(CONSTRUCTS_FIXTURE.read_text())["constructs"])


def _pattern_constructs(tree: Node) -> set[str]:
    """The construct ids a lexer tree holds that a validity row can describe."""
    ids: set[str] = set()
    for node in tool.walk(tree):
        if node.kind == "anchor":
            ids.add(
                {"\\z": "end-of-string-z", "\\Z": "end-of-string-Z", "\\A": "start-A"}.get(
                    str(node.detail["kind"]), ""
                )
            )
    ids.discard("")
    return ids


def python_invalid_minors(pattern: str, rows: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    """The supported minors on which a construct of ``pattern`` does not compile."""
    try:
        present = _pattern_constructs(tool.lex(pattern))
    except tool.UnknownConstruct:
        return ()
    invalid: set[str] = set()
    for row in rows:
        if row.get("id") not in present:
            continue
        compiles = row.get("python_compiles")
        for minor in SUPPORTED_PYTHON_MINORS:
            value = compiles.get(minor, False) if isinstance(compiles, Mapping) else compiles
            if not value:
                invalid.add(minor)
    return tuple(minor for minor in SUPPORTED_PYTHON_MINORS if minor in invalid)


def consumer_flags(consumer: str) -> int:
    flags = tool.CONSUMER_FLAGS.get(consumer, frozenset())
    value = 0
    if "case_insensitive" in flags:
        value |= re.IGNORECASE
    if "multi_line" in flags:
        value |= regex.MULTILINE if tool.CONSUMER_ENGINES[consumer] == "regex" else re.MULTILINE
    return value


def _engine(consumer: str) -> Any:
    """The consumer's Python regex module; ``re`` and ``regex`` share the calls used here."""
    return regex if tool.CONSUMER_ENGINES[consumer] == "regex" else re


@functools.cache
def _compiled(consumer: str, pattern: str, without: str | None = None) -> Any:
    flags = consumer_flags(consumer)
    if without == "case_insensitive":
        flags &= ~re.IGNORECASE
    elif without == "multi_line":
        flags &= ~re.MULTILINE
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        return _engine(consumer).compile(pattern, flags)


@functools.cache
def _all_scalars() -> str:
    return "".join(chr(p) for low, high in ALL_SCALARS for p in range(low, high + 1))


def _index_point(index: int) -> int:
    return index if index < 0xD800 else index + 0x800


@functools.cache
def python_membership(atom: str, engine: Literal["re", "regex"]) -> tuple[tuple[int, int], ...]:
    """The scalar values a one-character atom fullmatches on one Python engine."""
    module = regex if engine == "regex" else re
    flags = regex.MULTILINE if engine == "regex" else 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        compiled = module.compile(f"(?:{atom})", flags)
    subject = _all_scalars()
    points = []
    for found in compiled.finditer(subject):
        start, end = found.span()
        if end - start == 1:
            points.append(_index_point(start))
        elif end - start > 1:
            raise ValueError(f"{atom!r} is not a one-character atom")
    return _merge(points)


def python_boundary(atom: str, engine: Literal["re", "regex"], subject: str) -> tuple[int, ...]:
    """The offsets of ``subject`` at which a zero-width atom holds on one Python engine."""
    module = regex if engine == "regex" else re
    flags = regex.MULTILINE if engine == "regex" else 0
    compiled = module.compile(atom, flags)
    return tuple(
        found.start() for found in compiled.finditer(subject) if found.start() == found.end()
    )


def membership_digest(ranges: Sequence[tuple[int, int]]) -> str:
    rendered = json.dumps([[low, high] for low, high in ranges], separators=(",", ":"))
    return hashlib.sha256(rendered.encode("ascii")).hexdigest()


def group_spans(match: re.Match[str]) -> tuple[tuple[int, int] | None, ...]:
    """One span per group, None for a group that did not take part (Python reports -1, -1)."""
    spans = (match.span(index) for index in range(1, (match.re.groups or 0) + 1))
    return tuple(None if span == (-1, -1) else span for span in spans)


# ---------------------------------------------------------------------------------------------
# Observables
# ---------------------------------------------------------------------------------------------


def _mode(consumer: str) -> str:
    return tool.CONSUMER_MODES.get(consumer, "search")


def observable(consumer: str, pattern: str, text: str, *, without: str | None = None) -> object:
    """What the consumer reads from one Python run of ``pattern`` on ``text``."""
    compiled = _compiled(consumer, pattern, without)
    mode = _mode(consumer)
    if mode == "sub":
        found = tuple(((m.start(), m.end()), group_spans(m)) for m in compiled.finditer(text))
        replaced = compiled.sub(lambda _m: tool.SUB_REPLACEMENT, text)
        return found, replaced
    match = getattr(compiled, mode)(text)
    if consumer == "SPAN-ALLOWLIST":
        return None if match is None else match.span()
    if consumer in ("MATCH-GROUPS-FIRSTLINE", "SEARCH-GROUP"):
        if match is None:
            return (False, None, ())
        return (True, match.span(), group_spans(match))
    return match is not None


def _single_shape(value: object) -> tuple[object, object, object] | None:
    if isinstance(value, tuple) and len(value) == 3:
        return (value[0], value[1], value[2])
    return None


def compare_observable(
    consumer: str, python_observable: object, driver_result: object
) -> str | None:
    """None when the driver's probe result gives the consumer the Python observable."""
    if _mode(consumer) == "sub":
        if driver_result != python_observable:
            return f"sub: Python {python_observable!r}, Rust {driver_result!r}"
        return None
    single = _single_shape(driver_result)
    if single is None:
        return f"not a single-match result: {driver_result!r}"
    matched, span, groups = single
    if consumer == "SPAN-ALLOWLIST":
        rust: object = span if matched else None
    elif consumer in ("MATCH-GROUPS-FIRSTLINE", "SEARCH-GROUP"):
        rust = (matched, span, groups) if matched else (False, None, ())
    else:
        rust = bool(matched)
    if rust != python_observable:
        return f"Python {python_observable!r}, Rust {rust!r}"
    return None


def check_rewrite(
    original: str, python_text: str, consumer: str, inputs: Iterable[str]
) -> str | None:
    """The first input on which the rewrite's observable differs from the original's."""
    for text in inputs:
        if observable(consumer, original, text) != observable(consumer, python_text, text):
            return text
    return None


def exhaustive_inputs(alphabet: str, max_len: int) -> Iterator[str]:
    for length in range(max_len + 1):
        for letters in itertools.product(alphabet, repeat=length):
            yield "".join(letters)


def matches_everything(python_text: str, consumer: str) -> bool:
    """A search consumer whose pattern has an alternative that matches the empty string anywhere."""
    if _mode(consumer) != "search":
        return False
    tree = tool.lex(python_text)
    return any(_always_empty(alternative) for alternative in tool.top_level_alternatives(tree))


def _always_empty(node: Node) -> bool:
    if node.kind == "seq":
        return all(_always_empty(child) for child in node.children)
    if node.kind == "repeat":
        return node.detail["min"] == 0
    if node.kind == "group":
        return _always_empty(node.children[0])
    if node.kind == "alt":
        return any(_always_empty(child) for child in node.children)
    return False


# ---------------------------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------------------------

_CANDIDATES = "a0 !x_-.;/\nAZ9q\t,|&"


def _chars_for(node: Node, source: str) -> list[str]:
    text = source[node.span[0] : node.span[1]]
    flags = re.IGNORECASE if node.detail.get("ci") else 0
    try:
        compiled = re.compile(
            f"(?:{text})", flags | (re.DOTALL if node.detail.get("dotall") else 0)
        )
    except re.error:
        return ["a"]
    found = [c for c in _CANDIDATES if compiled.fullmatch(c)]
    return found[:2] or ["\u00e9"]


def _witnesses(node: Node, source: str, cap: int = 6) -> list[str]:
    kind = node.kind
    if kind == "literal":
        return [chr(int(str(node.detail["cp"])))]
    if kind in ("class", "shorthand", "any"):
        return _chars_for(node, source)
    if kind in ("anchor", "backref"):
        return [""]
    if kind == "look":
        if node.detail["negative"]:
            return [""]
        return ["", *_witnesses(node.children[0], source, cap)][:cap]
    if kind in ("group", "atomic", "pattern"):
        return _witnesses(node.children[0], source, cap)
    if kind in ("alt", "conditional"):
        merged: list[str] = []
        for child in node.children:
            merged += _witnesses(child, source, 2)
        return list(dict.fromkeys(merged))[:cap]
    if kind == "repeat":
        inner = _witnesses(node.children[0], source, 2)
        low = int(str(node.detail["min"]))
        high = node.detail["max"]
        counts = [low] + ([low + 1] if high is None or int(str(high)) > low else [])
        return list(dict.fromkeys(w * n for w in inner for n in counts))[:cap]
    combos = [""]
    for child in node.children:
        options = _witnesses(child, source, 3)
        combos = list(dict.fromkeys(a + b for a in combos for b in options))[:cap]
    return combos


def derive_probes(original: str, python_text: str, consumer: str, limit: int = 2000) -> list[str]:
    """Probe inputs: the empty string first, then witnesses and their variants for the class."""
    probes: list[str] = [""]
    seeds: list[str] = []
    alphabet: list[str] = []
    for text in dict.fromkeys((original, python_text)):
        try:
            tree = tool.lex(text)
        except tool.UnknownConstruct:
            continue
        seeds += _witnesses(tree, text, 12)
        alphabet += [chr(int(str(n.detail["cp"]))) for n in tool.walk(tree) if n.kind == "literal"]
        if tool.boundary_atoms(tree, consumer):
            probes += [s for group in tool.BOUNDARY_SUBJECTS.values() for s in group]
    flags = tool.CONSUMER_FLAGS.get(consumer, frozenset())
    for seed in dict.fromkeys(seeds):
        probes += [seed, "!" + seed, seed + "!", "a" + seed, seed + "a", "x" + seed, seed + "x"]
        probes += [seed + "\n", "\n" + seed, " " + seed, seed[1:], seed[:-1]]
        if _mode(consumer) == "sub":
            probes += [seed + " " + seed, seed + seed, seed + "\n" + seed]
        if "case_insensitive" in flags:
            probes.append(seed.swapcase())
        if "multi_line" in flags:
            probes += ["x\n" + seed, seed + "\nx"]
    letters = "".join(dict.fromkeys(alphabet + ["\n", "x"]))[:6]
    probes += list(exhaustive_inputs(letters, 3 if len(letters) <= 6 else 2))
    if tool.SUBJECT_DOMAINS.get(consumer) == "scalar-first-line":
        probes = [p for p in probes if "\n" not in p]
    return list(dict.fromkeys(probes))[:limit]


def work_entry(
    index: int, rust_text: str, consumer: str, probes: Sequence[str]
) -> dict[str, object]:
    flags = tool.CONSUMER_FLAGS.get(consumer, frozenset())
    entry: dict[str, object] = {
        "index": index,
        "text": rust_text,
        "flags": {
            "case_insensitive": "case_insensitive" in flags,
            "multi_line": "multi_line" in flags,
        },
        "mode": _mode(consumer),
        "probes": list(probes),
    }
    if entry["mode"] == "sub":
        entry["replacement"] = tool.SUB_REPLACEMENT
    return entry


# ---------------------------------------------------------------------------------------------
# The two-engine driver
# ---------------------------------------------------------------------------------------------

DRIVER_COMMAND = (
    "cargo",
    "test",
    "--locked",
    "--manifest-path",
    str(tool.REPO / "Cargo.toml"),
    "-p",
    "gobby-port",
    "--test",
    "suite",
    "--",
    "--ignored",
    "--exact",
    "regex_audit::run_generated_patterns",
)


def _span(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    low, high = value
    return (int(low), int(high))


def _driver_pattern(entry: Mapping[str, Any]) -> object:
    if "error" in entry:
        return {"error": entry["error"]}
    results: list[object] = []
    for result in entry.get("results", []):
        if "matches" in result:
            found = tuple(
                (_span(m["span"]), tuple(_span(g) for g in m["groups"])) for m in result["matches"]
            )
            results.append((found, result["replaced"]))
        else:
            groups = tuple(_span(g) for g in result["groups"])
            results.append((bool(result["matched"]), _span(result["span"]), groups))
    return tuple(results)


def run_two_engine_driver(
    patterns: Sequence[Mapping[str, object]],
    atoms: Sequence[Mapping[str, object]],
    boundaries: Sequence[Mapping[str, object]] = (),
) -> DriverResults:
    """Run the Rust driver once over every entry and read its results back by index."""
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch) / "work.json"
        out = Path(scratch) / "out.json"
        document = {
            "patterns": list(patterns),
            "atoms": list(atoms),
            "boundaries": list(boundaries),
        }
        work.write_text(json.dumps(document))
        environment = dict(
            os.environ, GOBBY_REGEX_AUDIT_WORK=str(work), GOBBY_REGEX_AUDIT_OUT=str(out)
        )
        run = subprocess.run(
            DRIVER_COMMAND, env=environment, capture_output=True, text=True, check=False
        )
        if run.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"the Rust driver failed:\n{run.stdout[-4000:]}\n{run.stderr[-4000:]}"
            )
        result = json.loads(out.read_text())
    pattern_results = {int(e["index"]): _driver_pattern(e) for e in result["patterns"]}
    atom_results = {
        int(e["index"]): tuple((int(a), int(b)) for a, b in e["ranges"])
        for e in result["atoms"]
        if "ranges" in e
    }
    boundary_results = {
        int(e["index"]): {s: tuple(int(o) for o in v) for s, v in e["offsets"].items()}
        for e in result["boundaries"]
        if "offsets" in e
    }
    return DriverResults(pattern_results, atom_results, boundary_results)


def domain_table(
    atoms: Iterable[tuple[str, str]],
    boundaries: Iterable[tuple[str, str]] = (),
    boundary_rows: Sequence[Mapping[str, Any]] = (),
    membership_rows: Mapping[str, Any] | None = None,
) -> DomainTable:
    """The measured table: Python sets per engine, Rust sets and offsets, and the digest rows."""
    python: dict[tuple[str, str], Ranges] = {}
    wanted: set[tuple[str, str]] = set(atoms)
    for engine, atom in list(wanted):
        node = tool.lex(atom)
        if node.kind == "class":
            items = cast(Sequence[str], node.detail["items"])
            wanted |= {(engine, str(item)) for item in items}
    boundary_list = sorted(set(boundaries))
    for engine, atom in boundary_list:
        wanted.add((engine, "(?i:\\w)" if atom.startswith("(?i:") else "\\w"))
    for engine, atom in sorted(wanted):
        python[(engine, atom)] = python_membership(atom, _engine_name(engine))
    texts = sorted({atom for _, atom in wanted})
    boundary_texts = sorted({atom for _, atom in boundary_list})
    subjects = [s for group in tool.BOUNDARY_SUBJECTS.values() for s in group]
    results = run_two_engine_driver(
        [],
        [{"index": i, "text": text} for i, text in enumerate(texts)],
        [{"index": i, "text": t, "subjects": subjects} for i, t in enumerate(boundary_texts)],
    )
    rust = {text: results.atoms[i] for i, text in enumerate(texts) if i in results.atoms}
    rust_boundary = {
        text: results.boundaries[i]
        for i, text in enumerate(boundary_texts)
        if i in results.boundaries
    }
    python_boundary: dict[tuple[str, str, str], dict[str, tuple[int, ...]]] = {}
    rows = {str(row["atom"]): row for row in boundary_rows}
    for engine, atom in boundary_list:
        for minor in SUPPORTED_PYTHON_MINORS:
            if minor == RUNNING_MINOR:
                python_boundary[(engine, minor, atom)] = {
                    s: python_boundary_offsets(atom, engine, s) for s in subjects
                }
            elif atom in rows:
                offsets = rows[atom]["offsets"]
                python_boundary[(engine, minor, atom)] = {
                    s: tuple(cast(Sequence[int], by_version(v[engine], minor)))
                    for s, v in offsets.items()
                    if by_version(v[engine], minor) is not None
                }
    return DomainTable(
        python, rust, python_boundary, rust_boundary, _digests(membership_rows or {})
    )


def _engine_name(engine: str) -> Literal["re", "regex"]:
    if engine not in ("re", "regex"):
        raise ValueError(f"no Python engine {engine!r}")
    return "regex" if engine == "regex" else "re"


def python_boundary_offsets(atom: str, engine: str, subject: str) -> tuple[int, ...]:
    return python_boundary(atom, _engine_name(engine), subject)


def _digests(membership: Mapping[str, Any]) -> dict[tuple[str, str, str], str]:
    digests: dict[tuple[str, str, str], str] = {}
    for row in membership.get("shorthand", ()):
        for minor in SUPPORTED_PYTHON_MINORS:
            value = by_version(row["digest"], minor)
            if isinstance(value, list):
                digests[(row["engine"], minor, row["atom"])] = str(value[1])
    low, high = tool.IGNORE_CASE_LITERALS
    for row in membership.get("ignore_case_ascii", ()):
        for minor in SUPPORTED_PYTHON_MINORS:
            value = by_version(row["block_sha256"], minor)
            if isinstance(value, str):
                for point in range(low, high + 1):
                    digests[(row["engine"], minor, f"(?i:\\x{point:02x})")] = value
    return digests


# The classifier, the families and verify are in regex_audit_classify.py, which imports this
# module; these entry points reach it at call time. The consumer drivers and the record
# functions are in regex_audit_record.py and are imported at the top of this module.


def classify(pattern: str, consumer: str, domain: DomainTable) -> Verdict:
    """The verdict for one pattern under its consumer class (regex_audit_classify.py)."""
    from tests.contracts import regex_audit_classify

    return regex_audit_classify.classify(pattern, consumer, domain)


def verify(rows: Sequence[tuple[FoundRegex, Verdict]]) -> list[tuple[FoundRegex, Verdict]]:
    """Every exact text through the two-engine driver (regex_audit_classify.py)."""
    from tests.contracts import regex_audit_classify

    return regex_audit_classify.verify(rows)


def literals_independent(x: str, y: str, flags: int, domain: DomainTable) -> bool | None:
    """Whether two R5 literals cannot overlap (regex_audit_classify.py)."""
    from tests.contracts import regex_audit_classify

    return regex_audit_classify.literals_independent(x, y, flags, domain)


def rust_safe(text: str) -> list[str]:
    """The construct ids in ``text`` Rust cannot take as written (regex_audit_classify.py)."""
    from tests.contracts import regex_audit_classify

    return regex_audit_classify.rust_safe(text)


def to_rust_text(python_text: str, consumer: str, domain: DomainTable) -> str:
    """The token translations of a Python text (regex_audit_classify.py)."""
    from tests.contracts import regex_audit_classify

    return regex_audit_classify.to_rust_text(python_text, consumer, domain)


__all__ = [
    "ALL_SCALARS",
    "DomainTable",
    "DriverResults",
    "FoundRegex",
    "Verdict",
    "by_version",
    "check_rewrite",
    "classify",
    "compare_observable",
    "construct_rows",
    "consumer_flags",
    "dataclasses",
    "derive_probes",
    "domain_table",
    "drive_consumer",
    "exhaustive_inputs",
    "group_spans",
    "literals_independent",
    "matches_everything",
    "membership_digest",
    "observable",
    "python_boundary",
    "python_invalid_minors",
    "python_membership",
    "record_consumer_cases",
    "record_regex_audit",
    "run_two_engine_driver",
    "rust_safe",
    "to_rust_text",
    "verify",
    "work_entry",
    "ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS",
]
