"""Python suppression discovery and decrease-only baseline support."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import token
import tokenize
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

type SuppressionDirective = Literal["noqa", "type: ignore"]

_BASELINE_SCHEMA_VERSION = 1
_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "node_modules",
        "site-packages",
        "target",
        "third_party",
        "vendor",
        "vendored",
        "vendors",
        "venv",
    }
)
_GENERATED_MARKERS = ("@generated", "automatically generated", "generated file", "do not edit")
_SUPPRESSION_RE = re.compile(
    r"^\#\s*(?:"
    r"(?P<type>type\s*:\s*ignore)(?:\s*\[(?P<type_codes>[^\]]*)\])?"
    r"|(?P<noqa>noqa)(?:\s*:\s*(?P<noqa_codes>[A-Z][A-Z0-9]*"
    r"(?:\s*,\s*[A-Z][A-Z0-9]*)*))?"
    r")(?:\s|$|\#|[-—])",
    re.IGNORECASE,
)
_COMPOUND_STATEMENTS = (
    ast.AsyncFor,
    ast.AsyncFunctionDef,
    ast.AsyncWith,
    ast.ClassDef,
    ast.For,
    ast.FunctionDef,
    ast.If,
    ast.Match,
    ast.Try,
    ast.While,
    ast.With,
)
_CLAUSE_PREFIXES = ("case ", "elif ", "else:", "except ", "except* ", "finally:")


@dataclass(frozen=True)
class SuppressionSite:
    """One real suppression directive found in a Python comment token."""

    path: str
    symbol: str
    directive: SuppressionDirective
    codes: tuple[str, ...]
    statement: str
    line: int

    @property
    def fingerprint(self) -> str:
        identity = {
            "codes": self.codes,
            "directive": self.directive,
            "path": self.path,
            "statement": self.statement,
            "symbol": self.symbol,
        }
        payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def baseline_entry(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "path": self.path,
            "symbol": self.symbol,
            "directive": self.directive,
            "codes": list(self.codes),
            "statement": self.statement,
        }


@dataclass(frozen=True)
class SuppressionScan:
    """Repository suppression scan and discovery totals."""

    sites: tuple[SuppressionSite, ...]
    files_scanned: int


@dataclass(frozen=True)
class SuppressionBaseline:
    """Validated suppression identities loaded from JSON."""

    entries: tuple[Mapping[str, object], ...]

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(str(entry["fingerprint"]) for entry in self.entries)


@dataclass(frozen=True)
class SuppressionDiff:
    """Occurrence-aware difference between current sites and the baseline."""

    new_sites: tuple[SuppressionSite, ...]
    stale_entries: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class _SymbolRange:
    start: int
    end: int
    name: str


def scan_suppressions(
    paths: Iterable[str | Path],
    *,
    root: Path,
) -> SuppressionScan:
    """Scan requested Python paths for real comment-token suppressions."""
    resolved_root = root.resolve()
    targets = tuple(_resolve_target(path, root=resolved_root) for path in paths)
    files = tuple(_discover_python_files(targets, root=resolved_root))
    sites = tuple(site for path in files for site in _scan_python_file(path, root=resolved_root))
    return SuppressionScan(
        sites=tuple(sorted(sites, key=_site_sort_key)),
        files_scanned=len(files),
    )


def load_suppression_baseline(path: Path) -> SuppressionBaseline:
    """Load and validate a suppression baseline."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read suppression baseline {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != _BASELINE_SCHEMA_VERSION:
        raise ValueError(
            f"Suppression baseline {path} must use schema_version {_BASELINE_SCHEMA_VERSION}"
        )
    entries = payload.get("sites")
    if not isinstance(entries, list):
        raise ValueError(f"Suppression baseline {path} must contain a sites list")
    validated = tuple(_validate_baseline_entry(entry, path=path) for entry in entries)
    declared_count = payload.get("site_count")
    if declared_count != len(validated):
        raise ValueError(
            f"Suppression baseline {path} site_count={declared_count!r} "
            f"does not match {len(validated)} sites"
        )
    return SuppressionBaseline(entries=validated)


def diff_suppressions(
    sites: Sequence[SuppressionSite],
    baseline: SuppressionBaseline,
) -> SuppressionDiff:
    """Compare sites using occurrence counts so identical statements remain distinct debt."""
    baseline_remaining = Counter(baseline.fingerprints)
    new_sites: list[SuppressionSite] = []
    for site in sites:
        if baseline_remaining[site.fingerprint] > 0:
            baseline_remaining[site.fingerprint] -= 1
        else:
            new_sites.append(site)

    current_remaining = Counter(site.fingerprint for site in sites)
    stale_entries: list[Mapping[str, object]] = []
    for entry in baseline.entries:
        fingerprint = str(entry["fingerprint"])
        if current_remaining[fingerprint] > 0:
            current_remaining[fingerprint] -= 1
        else:
            stale_entries.append(entry)
    return SuppressionDiff(tuple(new_sites), tuple(stale_entries))


def write_suppression_baseline(path: Path, sites: Sequence[SuppressionSite]) -> None:
    """Write deterministic suppression baseline JSON."""
    payload = {
        "schema_version": _BASELINE_SCHEMA_VERSION,
        "site_count": len(sites),
        "sites": [site.baseline_entry() for site in sorted(sites, key=_site_sort_key)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _resolve_target(path: str | Path, *, root: Path) -> Path:
    target = Path(path)
    resolved = target.resolve() if target.is_absolute() else (root / target).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Suppression scan path must be inside {root}: {path}") from exc
    if not resolved.exists():
        raise ValueError(f"Suppression scan path does not exist: {path}")
    return resolved


def _discover_python_files(targets: Sequence[Path], *, root: Path) -> Iterator[Path]:
    seen: set[Path] = set()
    for target in targets:
        if target.is_file():
            candidates: Iterable[Path] = (target,) if target.suffix in {".py", ".pyi"} else ()
        else:
            candidates = _walk_python_files(target, root=root)
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen or _is_generated_file(resolved):
                continue
            seen.add(resolved)
            yield resolved


def _walk_python_files(target: Path, *, root: Path) -> Iterator[Path]:
    for directory, dirnames, filenames in os.walk(target):
        directory_path = Path(directory)
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if not _is_excluded_directory(directory_path / dirname, root=root)
        ]
        for filename in sorted(filenames):
            path = directory_path / filename
            if path.suffix in {".py", ".pyi"}:
                yield path


def _is_excluded_directory(path: Path, *, root: Path) -> bool:
    return not _EXCLUDED_DIRECTORY_NAMES.isdisjoint(path.relative_to(root).parts)


def _is_generated_file(path: Path) -> bool:
    try:
        prefix = "".join(path.read_text(encoding="utf-8").splitlines(keepends=True)[:5])
        comments = (
            item.string.lower()
            for item in tokenize.generate_tokens(io.StringIO(prefix).readline)
            if item.type == token.COMMENT
        )
        return any(marker in comment for comment in comments for marker in _GENERATED_MARKERS)
    except (OSError, UnicodeDecodeError, tokenize.TokenError):
        return False


def _scan_python_file(path: Path, *, root: Path) -> Iterator[SuppressionSite]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Could not read Python source {path}: {exc}") from exc
    symbols, statements = _source_ranges(source)
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for item in tokens:
            if item.type != token.COMMENT:
                continue
            parsed = _parse_directive(item.string)
            if parsed is None:
                continue
            directive, codes = parsed
            line = item.start[0]
            yield SuppressionSite(
                path=path.relative_to(root).as_posix(),
                symbol=_containing_symbol(symbols, line),
                directive=directive,
                codes=codes,
                statement=_suppressed_statement(source, statements, line),
                line=line,
            )
    except (IndentationError, tokenize.TokenError) as exc:
        raise ValueError(f"Could not tokenize Python source {path}: {exc}") from exc


def _parse_directive(comment: str) -> tuple[SuppressionDirective, tuple[str, ...]] | None:
    match = _SUPPRESSION_RE.match(comment)
    if match is None:
        return None
    if match.group("type") is not None:
        directive: SuppressionDirective = "type: ignore"
        raw_codes = match.group("type_codes") or ""
    else:
        directive = "noqa"
        raw_codes = match.group("noqa_codes") or ""
    codes = tuple(sorted(code.strip() for code in raw_codes.split(",") if code.strip()))
    return directive, codes


def _source_ranges(source: str) -> tuple[tuple[_SymbolRange, ...], tuple[ast.stmt, ...]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return (), ()
    symbols: list[_SymbolRange] = []

    class SymbolVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _visit_symbol(
            self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ) -> None:
            self.stack.append(node.name)
            start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
            symbols.append(
                _SymbolRange(start, node.end_lineno or node.lineno, ".".join(self.stack))
            )
            self.generic_visit(node)
            self.stack.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_symbol(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_symbol(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_symbol(node)

    SymbolVisitor().visit(tree)
    statements = tuple(node for node in ast.walk(tree) if isinstance(node, ast.stmt))
    return tuple(symbols), statements


def _containing_symbol(symbols: Sequence[_SymbolRange], line: int) -> str:
    candidates = [symbol for symbol in symbols if symbol.start <= line <= symbol.end]
    if not candidates:
        return "<module>"
    return min(candidates, key=lambda item: (item.end - item.start, -item.start)).name


def _suppressed_statement(source: str, statements: Sequence[ast.stmt], line: int) -> str:
    physical_line = source.splitlines()[line - 1].lstrip()
    if physical_line.startswith(_CLAUSE_PREFIXES):
        return _normalize_tokens(physical_line)
    candidates = [
        statement
        for statement in statements
        if statement.lineno <= line <= (statement.end_lineno or statement.lineno)
    ]
    if not candidates:
        return _normalize_tokens(physical_line)
    statement = min(
        candidates,
        key=lambda item: ((item.end_lineno or item.lineno) - item.lineno, -item.lineno),
    )
    segment = ast.get_source_segment(source, statement) or physical_line
    if isinstance(statement, _COMPOUND_STATEMENTS) and line == statement.lineno:
        segment = _compound_header(segment)
    return _normalize_tokens(segment)


def _compound_header(source: str) -> str:
    pieces: list[str] = []
    depth = 0
    try:
        for item in tokenize.generate_tokens(io.StringIO(source).readline):
            if item.type in {token.ENDMARKER, token.INDENT}:
                break
            if item.type == token.OP:
                if item.string in "([{":
                    depth += 1
                elif item.string in ")]}" and depth:
                    depth -= 1
                elif item.string == ":" and depth == 0:
                    pieces.append(item.string)
                    break
            pieces.append(item.string)
    except (IndentationError, tokenize.TokenError):
        return source.splitlines()[0]
    return " ".join(pieces)


def _normalize_tokens(source: str) -> str:
    ignored = {
        token.COMMENT,
        token.DEDENT,
        token.ENCODING,
        token.ENDMARKER,
        token.INDENT,
        token.NEWLINE,
        tokenize.NL,
    }
    try:
        pieces = [
            item.string
            for item in tokenize.generate_tokens(io.StringIO(source).readline)
            if item.type not in ignored
        ]
    except (IndentationError, tokenize.TokenError):
        return " ".join(source.split())
    return " ".join(piece for piece in pieces if piece)


def _validate_baseline_entry(entry: Any, *, path: Path) -> Mapping[str, object]:
    if not isinstance(entry, dict):
        raise ValueError(f"Suppression baseline {path} contains a non-object site")
    expected = {"fingerprint", "path", "symbol", "directive", "codes", "statement"}
    if set(entry) != expected:
        raise ValueError(f"Suppression baseline {path} site fields must be {sorted(expected)}")
    if entry["directive"] not in {"noqa", "type: ignore"}:
        raise ValueError(f"Suppression baseline {path} contains an invalid directive")
    if not isinstance(entry["codes"], list) or not all(
        isinstance(code, str) for code in entry["codes"]
    ):
        raise ValueError(f"Suppression baseline {path} contains invalid codes")
    if not all(isinstance(entry[key], str) for key in expected - {"codes"}):
        raise ValueError(f"Suppression baseline {path} contains non-string site fields")
    identity = {
        "codes": tuple(entry["codes"]),
        "directive": entry["directive"],
        "path": entry["path"],
        "statement": entry["statement"],
        "symbol": entry["symbol"],
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    expected_fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    if entry["fingerprint"] != expected_fingerprint:
        raise ValueError(f"Suppression baseline {path} contains an invalid fingerprint")
    return entry


def _site_sort_key(site: SuppressionSite) -> tuple[object, ...]:
    return (site.path, site.symbol, site.directive, site.codes, site.statement, site.line)
