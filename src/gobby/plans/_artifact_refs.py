"""Shared artifact-reference matching for plan validation."""

from __future__ import annotations

import re
from pathlib import Path

from gobby.plans.parser import AcceptanceItem, ArtifactKind

_PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.:-]+)")
_TEST_REF_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.py)::"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
_BARE_FILE_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?P<file>[A-Za-z0-9_.-]+\.(?:md|py|ya?ml|toml|json))"
)
_ARTIFACT_MARKERS = ("file:", "symbol:", "test:", "behavior:")


def artifact_referenced(item: AcceptanceItem, validation_criteria: str) -> bool:
    if item.artifact_kind is ArtifactKind.file:
        return _file_referenced(item.artifact_ref, validation_criteria)
    if item.artifact_kind is ArtifactKind.symbol:
        return _symbol_referenced(item.artifact_ref, validation_criteria)
    if item.artifact_kind is ArtifactKind.test:
        return _test_referenced(item.artifact_ref, validation_criteria)
    if item.artifact_kind is ArtifactKind.behavior:
        return _behavior_referenced(item, validation_criteria)
    return False


def _file_referenced(artifact_ref: str, validation_criteria: str) -> bool:
    candidates = _path_candidates(artifact_ref) | _extract_path_candidates(artifact_ref)
    if any(_contains_ref(validation_criteria, candidate) for candidate in candidates):
        return True
    if any(_path_parts_referenced(validation_criteria, candidate) for candidate in candidates):
        return True
    markers = _artifact_markers(artifact_ref)
    return bool(markers) and all(marker in validation_criteria for marker in markers)


def _symbol_referenced(artifact_ref: str, validation_criteria: str) -> bool:
    candidates = {artifact_ref}
    if "." in artifact_ref:
        candidates.add(artifact_ref.rsplit(".", maxsplit=1)[-1])
    return any(
        _contains_ref(validation_criteria, candidate) for candidate in candidates if candidate
    )


def _test_referenced(artifact_ref: str, validation_criteria: str) -> bool:
    if _contains_ref(validation_criteria, artifact_ref):
        return True
    return any(
        (
            _contains_ref(validation_criteria, path)
            or _path_parts_referenced(validation_criteria, path)
        )
        and _contains_ref(validation_criteria, test_name)
        for path, test_name in _test_ref_candidates(artifact_ref)
    )


def _behavior_referenced(item: AcceptanceItem, validation_criteria: str) -> bool:
    if _contains_ref(validation_criteria, item.artifact_ref, case_sensitive=False):
        return True
    path_candidates = _extract_path_candidates(item.prose) | _extract_path_candidates(
        item.artifact_ref
    )
    path_candidates |= _bare_file_candidates(item.prose) | _bare_file_candidates(item.artifact_ref)
    if any(
        _contains_ref(validation_criteria, candidate)
        or _path_parts_referenced(validation_criteria, candidate)
        for candidate in path_candidates
    ):
        return True
    return any(
        _contains_ref(validation_criteria, task_ref, case_sensitive=False)
        for task_ref in re.findall(r"#\d+", item.artifact_ref)
    )


def _contains_ref(text: str, ref: str, *, case_sensitive: bool = True) -> bool:
    cleaned_ref = _clean_ref(ref)
    if not cleaned_ref:
        return False
    if case_sensitive:
        return cleaned_ref in text
    return cleaned_ref.lower() in text.lower()


def _path_candidates(artifact_ref: str) -> set[str]:
    cleaned = _clean_ref(artifact_ref).rstrip(".,;)")
    candidates = {cleaned}
    if cleaned.startswith("./"):
        candidates.add(cleaned[2:])

    path = Path(cleaned)
    if len(path.parts) >= 2:
        candidates.add("/".join(path.parts[-2:]))
    if path.is_absolute():
        for anchor in ("src", "tests", "docs", "web", "schemas", ".gobby"):
            if anchor in path.parts:
                candidates.add("/".join(path.parts[path.parts.index(anchor) :]))
                break
    return {candidate for candidate in candidates if candidate}


def _test_ref_candidates(artifact_ref: str) -> tuple[tuple[str, str], ...]:
    cleaned = _clean_ref(artifact_ref)
    matches = tuple(
        (match.group("path"), match.group("name")) for match in _TEST_REF_RE.finditer(cleaned)
    )
    if matches:
        return matches
    if "::" not in cleaned:
        return ()
    path, raw_test_name = cleaned.split("::", maxsplit=1)
    match = re.match(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)", raw_test_name)
    return ((path, match.group("name")),) if match is not None else ()


def _path_parts_referenced(text: str, path_value: str) -> bool:
    parts = [part for part in Path(_clean_ref(path_value)).parts if part not in {"", "."}]
    if len(parts) < 2:
        return False
    normalized_text = re.sub(r"[`'/\\]+", " ", text.casefold()).replace(chr(34), " ")
    position = 0
    for part in parts[-2:]:
        found = normalized_text.find(part.casefold(), position)
        if found == -1:
            return False
        position = found + len(part)
    return True


def _bare_file_candidates(text: str) -> set[str]:
    return {match.group("file") for match in _BARE_FILE_RE.finditer(text)}


def _artifact_markers(text: str) -> tuple[str, ...]:
    return tuple(marker for marker in _ARTIFACT_MARKERS if marker in text)


def _clean_ref(value: str) -> str:
    return value.strip().strip("`").strip(chr(34)).strip("'")


def _extract_path_candidates(text: str) -> set[str]:
    candidates: set[str] = set()
    for match in _PATH_RE.finditer(text):
        candidates.update(_path_candidates(match.group("path").rstrip(".,;)")))
    return candidates


__all__ = ["artifact_referenced"]
