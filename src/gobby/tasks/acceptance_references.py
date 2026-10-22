"""Parse test and file references in task acceptance criteria."""

from __future__ import annotations

import re

_ARTIFACT_REF_RE = re.compile(
    r"^\s*(?:>\s*)?(?:(?:[-*+]\s+|\d+[.)]\s+)"
    r"(?:[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)+:\s+)?)?"
    r"(?:\*\*)?(?P<kind>test|file)(?:\*\*)?:"
    r"(?:\s*`(?P<quoted>[^`]+)`|\s+(?P<bare>[^\s,;]+))",
    re.IGNORECASE | re.MULTILINE,
)


_INLINE_CRITERION_LINE_RE = re.compile(
    r"^[ \t]*(?:>[ \t]*)?(?:(?:[-*+][ \t]+|\d+[.)][ \t]+)?"
    r"[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)+:|(?P<numbered>\d+[.)]))"
    r"[ \t]+(?P<body>[^\n]*)",
    re.MULTILINE,
)


_INLINE_ARTIFACT_REF_RE = re.compile(
    r"(?<![\w-])(?P<kind>test|file):"
    r"(?:[ \t]*`(?P<quoted>[^`]+)`|[ \t]+(?P<bare>[^\s,;]+))",
    re.IGNORECASE,
)


_ILLUSTRATIVE_QUOTE_RE = re.compile(
    r"\b(?:such as|for example|example|e\.g\.)[ \t]*[:,]?[ \t]*"
    r"(?P<quote>'(?:[^'\n]|'(?=\w))*'|\"[^\"\n]*\")",
    re.IGNORECASE,
)


_BARE_REF_SHAPE_RE = re.compile(
    r"::|/|\.(?:py|rs|ts|tsx|js|mjs|cjs|sh|md|ya?ml|json|toml|sql)$",
)


def _trim_bare_reference(value: str) -> str:
    """Drop the ':', '.' or unbalanced ')' an unbackticked reference picks up from prose."""
    while value:
        if value[-1] in ".:":
            value = value[:-1]
        elif value[-1] == ")" and value.count(")") > value.count("("):
            value = value[:-1]
        else:
            break
    return value


def extract_artifact_references(criteria: str, kind: str) -> tuple[str, ...]:
    """Extract stable, deduplicated test or file references from criteria."""
    criteria = _mask_fenced_criteria(criteria)
    references: list[str] = []
    matches = [(match.start(), match) for match in _ARTIFACT_REF_RE.finditer(criteria)]
    for criterion in _INLINE_CRITERION_LINE_RE.finditer(criteria):
        body = criterion.group("body")
        inert_spans = [span.span() for span in re.finditer(r"`[^`\n]*`", body)]
        if criterion.group("numbered"):
            inert_spans.extend(
                example.span("quote") for example in _ILLUSTRATIVE_QUOTE_RE.finditer(body)
            )
        for match in _INLINE_ARTIFACT_REF_RE.finditer(body):
            if any(start <= match.start() < end for start, end in inert_spans):
                continue
            matches.append((criterion.start("body") + match.start(), match))
    for _, match in sorted(matches, key=lambda item: item[0]):
        if match.group("kind").casefold() != kind.casefold():
            continue
        value = (match.group("quoted") or match.group("bare") or "").strip().rstrip(".")
        if match.group("quoted") is None:
            value = _trim_bare_reference(value.rstrip("`"))
            if not _BARE_REF_SHAPE_RE.search(value):
                continue
        if value and value not in references:
            references.append(value)
    return tuple(references)


def _mask_fenced_criteria(criteria: str) -> str:
    """Keep fenced examples inert, including an unfinished fence."""
    fence = ""
    lines: list[str] = []
    for line in criteria.splitlines(keepends=True):
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})([^\n]*)", line)
        if marker and not fence and marker.group(1).startswith("`") and "`" in marker.group(2):
            marker = None
        masked = bool(fence or marker)
        if marker:
            delimiter, rest = marker.groups()
            if not fence:
                fence = delimiter
            elif delimiter[0] == fence[0] and len(delimiter) >= len(fence) and not rest.strip():
                fence = ""
        lines.append(re.sub(r"[^\n]", " ", line) if masked else line)
    return "".join(lines)


def malformed_test_reference_findings(criteria: str) -> tuple[str, ...]:
    """Return diagnostics for named tests that omit a path or symbol."""
    criteria = _mask_fenced_criteria(criteria)
    references = extract_artifact_references(criteria, "test")
    findings = [
        f"{reference}: malformed test reference; expected path::test_symbol"
        for reference in references
        if parse_test_reference(reference) is None
    ]
    inert_spans = [match.span() for match in re.finditer(r"`[^`\n]*`", criteria)]
    inert_spans.extend(match.span("quote") for match in _ILLUSTRATIVE_QUOTE_RE.finditer(criteria))
    for match in re.finditer(r"(?<![\w-])test:[ \t]*`([^`\n]+)`", criteria, re.IGNORECASE):
        if any(start <= match.start() < end for start, end in inert_spans):
            continue
        reference = match.group(1).strip().rstrip(".")
        if reference not in references:
            findings.append(f"{reference}: unrecognized test reference; use 'test:' at line start")
    return tuple(dict.fromkeys(findings))


def parse_test_reference(reference: str) -> tuple[str, str] | None:
    if "::" not in reference:
        return None
    path, symbol = reference.split("::", 1)
    path = path.strip()
    symbol = symbol.strip()
    if len(symbol) >= 2 and symbol[0] == symbol[-1] and symbol[0] in "\"'":
        symbol = symbol[1:-1].strip()
    return (path, symbol) if path and symbol else None
