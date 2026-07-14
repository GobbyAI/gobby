"""File-path normalization helpers for review-learning lessons."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from gobby.review_learning.fingerprint import short_hash
from gobby.utils.project_context import find_project_root

_LINE_REF_RE = re.compile(r"^(?P<path>.+?):\d+(?::\d+)?$")
_PATH_FIELD_NAMES = {
    "path",
    "paths",
    "file",
    "files",
    "file_path",
    "file_paths",
    "filename",
    "filenames",
    "changed_file",
    "changed_files",
    "evidence_path",
    "line_ref",
    "line_refs",
}


def normalize_lesson_file_path(value: Any) -> str:
    """Return a safe repo-relative lesson path for deterministic matching."""
    if value is None:
        return ""
    text = str(value).strip().strip("\"'")
    if not text:
        return ""
    if text.startswith("file://"):
        text = text[7:]
    text = text.replace("\\", "/")
    line_match = _LINE_REF_RE.match(text)
    if line_match:
        text = line_match.group("path")
    while text.startswith("./"):
        text = text[2:]

    path = PurePosixPath(text)
    if path.is_absolute():
        absolute_path = Path(text).resolve(strict=False)
        project_root = find_project_root(absolute_path)
        if project_root is None:
            return ""
        try:
            path = PurePosixPath(absolute_path.relative_to(project_root.resolve()).as_posix())
        except ValueError:
            return ""

    if ".." in path.parts:
        return ""
    return "/".join(part for part in path.parts if part not in {"", "."})


def path_tag(value: Any) -> str:
    """Return the bounded review-lesson tag for a normalized file path."""
    normalized = normalize_lesson_file_path(value)
    if not normalized:
        return ""
    return f"path:{short_hash(normalized)}"


def paths_match(touched_path: Any, lesson_path: Any) -> bool:
    """Return true when two safe repo-relative paths are exactly equal."""
    touched = normalize_lesson_file_path(touched_path)
    lesson = normalize_lesson_file_path(lesson_path)
    if not touched or not lesson:
        return False
    return touched == lesson


def extract_lesson_file_paths(
    *,
    finding: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> list[str]:
    """Extract file paths from review lesson finding/evidence payloads."""
    paths: list[str] = []
    if finding:
        paths.extend(extract_file_paths_from_mapping(finding))
    if evidence:
        paths.extend(extract_file_paths_from_mapping(evidence))
    return _dedupe_paths(paths)


def extract_file_paths_from_mapping(payload: Mapping[str, Any]) -> list[str]:
    """Extract path-like values from known path-bearing fields."""
    paths: list[str] = []
    for key, value in payload.items():
        if _is_path_field(key):
            paths.extend(_coerce_path_values(value))
        elif isinstance(value, Mapping):
            paths.extend(extract_file_paths_from_mapping(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    paths.extend(extract_file_paths_from_mapping(item))
    return _dedupe_paths(paths)


def _is_path_field(key: Any) -> bool:
    text = str(key).strip().lower()
    return (
        text in _PATH_FIELD_NAMES
        or text.endswith("_path")
        or text.endswith("_paths")
        or text.endswith("_file")
        or text.endswith("_files")
    )


def _coerce_path_values(value: Any) -> list[str]:
    if isinstance(value, str):
        normalized = normalize_lesson_file_path(value)
        return [normalized] if normalized else []
    if isinstance(value, Mapping):
        return extract_file_paths_from_mapping(value)
    if isinstance(value, list):
        paths: list[str] = []
        for item in value:
            paths.extend(_coerce_path_values(item))
        return paths
    return []


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for path in paths:
        normalized = normalize_lesson_file_path(path)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped
