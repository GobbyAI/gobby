"""Filename-based related-test discovery used by task expansion."""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

RELATED_TEST_MAX_FILES = 5
_STOPWORDS = frozenset(
    {
        "add",
        "added",
        "all",
        "and",
        "are",
        "assert",
        "can",
        "check",
        "code",
        "criteria",
        "description",
        "ensure",
        "file",
        "files",
        "fix",
        "for",
        "from",
        "has",
        "have",
        "include",
        "includes",
        "into",
        "must",
        "new",
        "not",
        "pass",
        "passes",
        "read",
        "related",
        "should",
        "src",
        "that",
        "task",
        "the",
        "this",
        "test",
        "tests",
        "with",
        "work",
        "works",
    }
)


def derive_related_test_terms(
    task_title: str,
    validation_criteria: str | None = None,
    task_description: str | None = None,
    *,
    max_terms: int = 24,
) -> list[str]:
    """Derive bounded filename terms for task-expansion test discovery."""
    search_text = f"{task_title} {validation_criteria or ''} {task_description or ''}"
    candidates = _iter_search_terms(search_text)
    for pattern in _extract_file_patterns(search_text):
        candidates.extend(_iter_search_terms(pattern))
        candidates.extend(_iter_search_terms(Path(pattern).stem))

    terms: list[str] = []
    for term in candidates:
        if term not in terms:
            terms.append(term)
        if len(terms) >= max_terms:
            break
    return terms


def find_related_test_files(
    search_terms: list[str],
    base_dir: str | Path = ".",
    max_files: int = RELATED_TEST_MAX_FILES,
) -> list[Path]:
    """Find test files whose path names match derived task search terms."""
    if not search_terms or max_files <= 0:
        return []
    base = Path(base_dir)
    tests_dir = base / "tests"
    if not tests_dir.is_dir():
        return []

    try:
        candidates = {
            path
            for pattern in ("test_*.py", "*_test.py")
            for path in tests_dir.rglob(pattern)
            if path.is_file()
        }
    except OSError as exc:
        logger.debug("Failed to search related test files: %s", exc)
        return []

    scored: list[tuple[int, str, Path]] = []
    for path in candidates:
        relative = path.relative_to(base).as_posix()
        relative_folded = relative.casefold()
        path_terms = set(_iter_search_terms(relative))
        score = sum(
            3 if term in path_terms else 1 if term in relative_folded else 0
            for term in search_terms
        )
        if score:
            scored.append((score, relative, path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _score, _relative, path in scored[:max_files]]


def _extract_file_patterns(text: str) -> list[str]:
    patterns = {
        match.lstrip("./")
        for match in re.findall(r"[./]?[\w-]+(?:/[\w-]+)*\.\w+", text)
        if not match.startswith(("http", "www."))
    }
    patterns.update(
        "src/" + module.replace(".", "/") + ".py"
        for module in re.findall(r"\b(gobby(?:\.\w+)+)\b", text)
    )
    patterns.update(f"tests/**/test_{name}*.py" for name in re.findall(r"\btest_(\w+)\b", text))
    return sorted(patterns)


def _iter_search_terms(text: str) -> list[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [
        term
        for token in re.split(r"[^A-Za-z0-9]+", separated)
        if (term := token.strip().casefold())
        and len(term) >= 3
        and not term.isdigit()
        and term not in _STOPWORDS
    ]


__all__ = ["derive_related_test_terms", "find_related_test_files"]
