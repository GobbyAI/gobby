"""Composable compression primitives.

Four primitives that can be chained in any order:
- filter_lines: Remove noise lines matching regex patterns
- group_lines: Aggregate lines by key (status, rule, file, directory)
- truncate: Keep head + tail, omit middle
- dedup: Collapse consecutive repeated/similar lines
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict

_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile and cache a regex pattern."""
    compiled = _PATTERN_CACHE.get(pattern)
    if compiled is None:
        compiled = re.compile(pattern)
        _PATTERN_CACHE[pattern] = compiled
    return compiled


def filter_lines(lines: list[str], *, patterns: list[str]) -> list[str]:
    """Remove lines matching any of the given regex patterns.

    Args:
        lines: Input lines (with newlines preserved).
        patterns: List of regex patterns. Lines matching any pattern are removed.

    Returns:
        Filtered lines.
    """
    compiled = [_compile_pattern(p) for p in patterns]
    return [line for line in lines if not any(r.search(line) for r in compiled)]


def group_lines(lines: list[str], *, mode: str) -> list[str]:
    """Group/aggregate lines by a key derived from the mode.

    Supported modes:
    - git_status: Group by status letter (M/A/D/??)
    - pytest_failures / test_failures: Extract failure blocks only
    - lint_by_rule: Group by lint rule/code
    - by_extension: Group files by extension
    - by_directory: Group paths by parent directory
    - by_file: Group grep-style "file:line" matches by file
    - errors_warnings: Separate errors and warnings

    Args:
        lines: Input lines.
        mode: Grouping mode name.

    Returns:
        Grouped/reorganized lines.
    """
    handler = _GROUP_HANDLERS.get(mode)
    if handler is None:
        return lines
    return handler(lines)


def truncate(
    lines: list[str],
    *,
    head: int = 20,
    tail: int = 10,
    per_file_lines: int = 0,
    file_marker: str = "",
) -> list[str]:
    """Keep first N + last M lines, replace middle with omission marker.

    If per_file_lines > 0 and file_marker is set, truncation happens
    per-section (delimited by file_marker matches) instead of globally.

    Args:
        lines: Input lines.
        head: Number of lines to keep from the start.
        tail: Number of lines to keep from the end.
        per_file_lines: If > 0, truncate per section.
        file_marker: Regex marking section boundaries.

    Returns:
        Truncated lines.
    """
    if per_file_lines > 0 and file_marker:
        return _truncate_per_section(lines, per_file_lines, file_marker)

    total = len(lines)
    if total <= head + tail:
        return lines

    omitted = total - head - tail
    result = lines[:head]
    result.append(f"\n[... {omitted} lines omitted ...]\n\n")
    result.extend(lines[-tail:])
    return result


def dedup(lines: list[str], **_kwargs: object) -> list[str]:
    """Collapse consecutive identical or near-identical lines.

    Near-identical: lines that differ only in numbers (e.g. progress counters,
    timestamps, line numbers in repeated warnings).

    Args:
        lines: Input lines.

    Returns:
        Deduplicated lines with count annotations.
    """
    if not lines:
        return lines

    result: list[str] = []
    prev_normalized: str | None = None
    prev_line: str = ""
    count: int = 0

    for line in lines:
        normalized = _DEDUP_NUMBER_RE.sub("N", line.strip())
        if normalized == prev_normalized:
            count += 1
        else:
            if count > 1:
                result.append(f"  [repeated {count} times]\n")
            elif count == 1:
                result.append(prev_line)
            prev_normalized = normalized
            prev_line = line
            count = 1

    # Flush last group
    if count > 1:
        result.append(prev_line)
        result.append(f"  [repeated {count} times]\n")
    elif count == 1:
        result.append(prev_line)

    return result


# --- Dedup helpers ---
_DEDUP_NUMBER_RE = re.compile(r"\d+")


# --- Truncation helpers ---


def _truncate_per_section(lines: list[str], max_lines: int, marker_pattern: str) -> list[str]:
    """Truncate each section (delimited by marker) independently."""
    marker_re = re.compile(marker_pattern)
    sections: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if marker_re.search(line) and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)

    result: list[str] = []
    for section in sections:
        if len(section) > max_lines:
            top = (max_lines + 1) // 2
            bottom = max_lines - top
            omitted = len(section) - max_lines
            result.extend(section[:top])
            result.append(f"  [... {omitted} lines omitted in section ...]\n")
            result.extend(section[-bottom:] if bottom > 0 else [])
        else:
            result.extend(section)
    return result


# --- Group handlers ---


_GIT_STATUS_RE = re.compile(r"^[\t ]*([MADRCU?! ]{1,2})\s+(.+)$")


def _group_git_status(lines: list[str]) -> list[str]:
    """Group git status output by status letter."""
    groups: dict[str, list[str]] = OrderedDict()
    other: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        m = _GIT_STATUS_RE.match(stripped)
        if m:
            status = m.group(1).strip()
            filename = m.group(2).strip()
            groups.setdefault(status, []).append(filename + "\n")
        else:
            other.append(line)

    result: list[str] = []
    status_labels = {
        "M": "Modified",
        "A": "Added",
        "D": "Deleted",
        "R": "Renamed",
        "C": "Copied",
        "??": "Untracked",
        "U": "Unmerged",
    }
    for status, files in groups.items():
        label = status_labels.get(status, status)
        result.append(f"{label} ({len(files)}):\n")
        for f in files[:20]:
            result.append(f"  {f}")
        if len(files) > 20:
            result.append(f"  [... and {len(files) - 20} more]\n")
    result.extend(other)
    return result


def _group_test_failures(lines: list[str]) -> list[str]:
    """Extract failure/error blocks from test output."""
    result: list[str] = []
    in_failure = False
    failure_markers = [
        re.compile(r"^FAIL"),
        re.compile(r"^FAILED"),
        re.compile(r"^ERROR"),
        re.compile(r"^E\s+"),
        re.compile(r"^---\s*FAIL"),
        re.compile(r"failures?:", re.IGNORECASE),
    ]
    end_markers = [
        re.compile(r"^=+ ?short test summary"),
        re.compile(r"^=+\s*\d+ (?:passed|failed)"),
        re.compile(r"^FAIL\s*$"),
    ]

    for line in lines:
        stripped = line.strip()
        if any(m.search(stripped) for m in failure_markers):
            in_failure = True
        if any(m.search(stripped) for m in end_markers):
            in_failure = True  # Include summary lines too
        if in_failure:
            result.append(line)

    # If no failures found, return summary line(s) only
    if not result:
        for line in lines:
            stripped = line.strip()
            if re.search(r"\d+\s+(?:passed|failed|error)", stripped, re.IGNORECASE):
                result.append(line)
        if not result:
            result.append("All tests passed.\n")

    return result


def _group_pytest_failures(lines: list[str]) -> list[str]:
    """Extract pytest failure blocks and short summary."""
    result: list[str] = []
    in_failure_section = False
    in_summary = False

    for line in lines:
        stripped = line.strip()
        # Capture FAILURES section
        if re.match(r"^=+ FAILURES =+", stripped):
            in_failure_section = True
            result.append(line)
            continue
        if re.match(r"^=+ short test summary", stripped):
            in_failure_section = False
            in_summary = True
            result.append(line)
            continue
        if in_summary and re.match(r"^=+", stripped):
            result.append(line)
            in_summary = False
            continue
        if in_failure_section or in_summary:
            result.append(line)
            continue
        # Always capture the final summary line
        if re.match(r"^=+.*(?:passed|failed|error|warning)", stripped):
            result.append(line)

    if not result:
        return _group_test_failures(lines)
    return result


def _group_lint_by_rule(lines: list[str]) -> list[str]:
    """Group lint errors by rule/code."""
    # Patterns for common lint output formats:
    # ruff: path:line:col: CODE message
    # mypy: path:line: error: message [code]
    # eslint: path:line:col  error  message  rule-name
    rule_re = re.compile(
        r"(?:"
        r":\s*([A-Z]\d{3,4})\s"  # ruff-style CODE
        r"|"
        r"\[([a-z-]+)\]\s*$"  # mypy [code]
        r"|"
        r"\s{2,}(\S+)\s*$"  # eslint rule-name at end
        r")"
    )

    groups: dict[str, list[str]] = OrderedDict()
    other: list[str] = []

    for line in lines:
        m = rule_re.search(line)
        if m:
            rule = m.group(1) or m.group(2) or m.group(3) or "unknown"
            groups.setdefault(rule, []).append(line)
        else:
            other.append(line)

    if not groups:
        return lines

    result: list[str] = []
    for rule, rule_lines in groups.items():
        result.append(f"[{rule}] ({len(rule_lines)} occurrences):\n")
        for rl in rule_lines[:5]:
            result.append(f"  {rl.strip()}\n")
        if len(rule_lines) > 5:
            result.append(f"  [... and {len(rule_lines) - 5} more]\n")
    result.extend(other)
    return result


def _group_by_extension(lines: list[str]) -> list[str]:
    """Group file listings by extension."""
    groups: dict[str, list[str]] = OrderedDict()
    other: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        _, ext = os.path.splitext(stripped.split()[-1] if stripped.split() else "")
        ext = ext or "(no ext)"
        groups.setdefault(ext, []).append(stripped + "\n")

    if not groups:
        return lines

    result: list[str] = []
    for ext, files in sorted(groups.items(), key=lambda x: -len(x[1])):
        result.append(f"{ext} ({len(files)} files):\n")
        for f in files[:10]:
            result.append(f"  {f}")
        if len(files) > 10:
            result.append(f"  [... and {len(files) - 10} more]\n")
    result.extend(other)
    return result


def _group_by_directory(lines: list[str]) -> list[str]:
    """Group paths by parent directory."""
    groups: dict[str, list[str]] = OrderedDict()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.rsplit("/", 1)
        dirname = parts[0] if len(parts) > 1 else "."
        groups.setdefault(dirname, []).append(stripped + "\n")

    if not groups:
        return lines

    result: list[str] = []
    for dirname, files in sorted(groups.items(), key=lambda x: -len(x[1])):
        result.append(f"{dirname}/ ({len(files)} items):\n")
        for f in files[:10]:
            result.append(f"  {f}")
        if len(files) > 10:
            result.append(f"  [... and {len(files) - 10} more]\n")
    return result


def _group_by_file(lines: list[str]) -> list[str]:
    """Group grep-style output by file."""
    groups: dict[str, list[str]] = OrderedDict()
    other: list[str] = []

    for line in lines:
        # grep format: file:line:content or file:content
        m = re.match(r"^([^:]+:\d+):", line)
        if m:
            filepath = line.split(":")[0]
            groups.setdefault(filepath, []).append(line)
        else:
            other.append(line)

    if not groups:
        return lines

    result: list[str] = []
    for filepath, matches in groups.items():
        count = len(matches)
        result.append(f"{filepath} ({count} {'match' if count == 1 else 'matches'}):\n")
        for ml in matches[:5]:
            result.append(f"  {ml.strip()}\n")
        if len(matches) > 5:
            result.append(f"  [... and {len(matches) - 5} more]\n")
    result.extend(other)
    return result


def _group_errors_warnings(lines: list[str]) -> list[str]:
    """Separate and summarize errors vs warnings from build output."""
    errors: list[str] = []
    warnings: list[str] = []
    other: list[str] = []

    error_re = re.compile(r"\berror\b", re.IGNORECASE)
    warn_re = re.compile(r"\bwarn(?:ing)?\b", re.IGNORECASE)

    for line in lines:
        if error_re.search(line):
            errors.append(line)
        elif warn_re.search(line):
            warnings.append(line)
        else:
            other.append(line)

    result: list[str] = []
    if errors:
        result.append(f"Errors ({len(errors)}):\n")
        result.extend(errors[:20])
        if len(errors) > 20:
            result.append(f"  [... and {len(errors) - 20} more errors]\n")
    if warnings:
        result.append(f"\nWarnings ({len(warnings)}):\n")
        result.extend(warnings[:10])
        if len(warnings) > 10:
            result.append(f"  [... and {len(warnings) - 10} more warnings]\n")
    if not errors and not warnings:
        return lines
    # Include last few non-error/warning lines (usually summary)
    if other:
        result.extend(other[-3:])
    return result


# Handler registry
_GROUP_HANDLERS = {
    "git_status": _group_git_status,
    "pytest_failures": _group_pytest_failures,
    "test_failures": _group_test_failures,
    "lint_by_rule": _group_lint_by_rule,
    "by_extension": _group_by_extension,
    "by_directory": _group_by_directory,
    "by_file": _group_by_file,
    "errors_warnings": _group_errors_warnings,
}
