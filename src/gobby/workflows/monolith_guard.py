"""Line-budget projection and completion checks for the optional monolith guard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MONOLITH_LINE_LIMIT = 1_000
MONOLITH_SOURCE_EXTENSIONS = frozenset(
    {".py", ".ts", ".tsx", ".css", ".rs", ".js", ".mjs", ".cjs", ".sh"}
)

_EXCLUDED_PATH_PARTS = frozenset(
    {
        "__fixtures__",
        "__tests__",
        ".venv",
        "baseline",
        "baselines",
        "build",
        "dist",
        "docs",
        "documentation",
        "fixture",
        "fixtures",
        "generated",
        "node_modules",
        "site-packages",
        "target",
        "test",
        "tests",
        "third-party",
        "third_party",
        "vendor",
        "vendors",
        "venv",
    }
)
_TARGETED_EDIT_KEYS = (
    ("old_string", "new_string"),
    ("old_text", "new_text"),
    ("old", "new"),
)


@dataclass
class _FileProjection:
    relative_path: str
    current_count: int
    projected_count: int
    text: str | None


@dataclass
class _PatchOperation:
    kind: str
    path: str
    destination: str | None = None
    added: int = 0
    removed: int = 0


def is_monolith_guard_path(path: str) -> bool:
    """Return whether a repo-relative path is hand-maintained production source."""
    normalized = path.replace("\\", "/").strip()
    if not normalized:
        return False

    pure_path = PurePosixPath(normalized)
    parts = [part.lower() for part in pure_path.parts]
    if pure_path.suffix.lower() not in MONOLITH_SOURCE_EXTENSIONS:
        return False
    if any(part in _EXCLUDED_PATH_PARTS for part in parts[:-1]):
        return False

    filename = parts[-1]
    stem = PurePosixPath(filename).stem
    test_markers = (
        stem == "conftest",
        stem.startswith("test_"),
        stem.endswith("_test"),
        stem.endswith("-test"),
        ".test." in filename,
        ".spec." in filename,
    )
    generated_markers = (
        stem.startswith("generated_"),
        stem.endswith("_generated"),
        stem.endswith("_baseline"),
        ".generated." in filename,
        ".gen." in filename,
        ".baseline." in filename,
    )
    return not any((*test_markers, *generated_markers))


def projected_monolith_paths(
    tool_input: Any,
    project_path: str | Path | None,
    event_data: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return touched paths whose current or projected count reaches the ceiling."""
    root = _project_root(project_path)
    if root is None:
        return []

    projections: dict[str, _FileProjection] = {}
    payload = tool_input if isinstance(tool_input, Mapping) else {}
    patch = payload.get("patch")
    if isinstance(patch, str):
        _apply_patch_projection(projections, root, patch)
    else:
        changes = payload.get("changes")
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, Mapping):
                    _apply_change_projection(projections, root, change)
        else:
            _apply_change_projection(projections, root, payload)

    for path in _fallback_paths(payload, event_data):
        _get_projection(projections, root, path)

    return [
        projection.relative_path
        for projection in projections.values()
        if projection.current_count >= MONOLITH_LINE_LIMIT
        or projection.projected_count >= MONOLITH_LINE_LIMIT
    ]


def outstanding_monolith_paths(
    variables: Mapping[str, Any],
    project_path: str | Path | None,
) -> list[str]:
    """Return over-budget files attributed to tasks touched by this session."""
    root = _project_root(project_path)
    task_files = variables.get("task_edited_files")
    if root is None or not isinstance(task_files, Mapping):
        return []

    paths: list[str] = []
    for raw_paths in task_files.values():
        if not isinstance(raw_paths, list):
            continue
        for raw_path in raw_paths:
            if not isinstance(raw_path, str):
                continue
            projection = _projection_for_path(root, raw_path)
            if (
                projection is not None
                and projection.relative_path not in paths
                and projection.current_count >= MONOLITH_LINE_LIMIT
            ):
                paths.append(projection.relative_path)
    return paths


def _project_root(project_path: str | Path | None) -> Path | None:
    if not project_path:
        return None
    try:
        root = Path(project_path).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return root if root.is_dir() else None


def _projection_for_path(root: Path, raw_path: str) -> _FileProjection | None:
    normalized = raw_path.replace("\\", "/").strip()
    if not normalized:
        return None
    candidate = Path(normalized)
    absolute = candidate if candidate.is_absolute() else root / candidate
    try:
        absolute = absolute.resolve()
        relative = absolute.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    if not is_monolith_guard_path(relative):
        return None

    text: str | None = None
    if absolute.is_file():
        try:
            text = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    count = _line_count(text)
    return _FileProjection(relative, count, count, text)


def _get_projection(
    projections: dict[str, _FileProjection],
    root: Path,
    raw_path: Any,
) -> _FileProjection | None:
    if not isinstance(raw_path, str):
        return None
    projection = _projection_for_path(root, raw_path)
    if projection is None:
        return None
    return projections.setdefault(projection.relative_path, projection)


def _apply_change_projection(
    projections: dict[str, _FileProjection],
    root: Path,
    change: Mapping[str, Any],
) -> None:
    raw_path = change.get("file_path") or change.get("path")
    projection = _get_projection(projections, root, raw_path)
    if projection is None:
        return

    content = change.get("content")
    if isinstance(content, str):
        projection.text = content
        projection.projected_count = _line_count(content)
        return

    for old_key, new_key in _TARGETED_EDIT_KEYS:
        old_text = change.get(old_key)
        new_text = change.get(new_key)
        if isinstance(old_text, str) and isinstance(new_text, str):
            _apply_targeted_edit(
                projection,
                old_text,
                new_text,
                replace_all=bool(change.get("replace_all")),
            )
            return


def _apply_targeted_edit(
    projection: _FileProjection,
    old_text: str,
    new_text: str,
    *,
    replace_all: bool,
) -> None:
    if projection.text is not None and old_text in projection.text:
        count = -1 if replace_all else 1
        projection.text = projection.text.replace(old_text, new_text, count)
        projection.projected_count = _line_count(projection.text)
        return

    occurrence_count = projection.text.count(old_text) if projection.text and replace_all else 1
    delta = (_line_count(new_text) - _line_count(old_text)) * occurrence_count
    projection.projected_count = max(0, projection.projected_count + delta)
    projection.text = None


def _apply_patch_projection(
    projections: dict[str, _FileProjection],
    root: Path,
    patch: str,
) -> None:
    for operation in _parse_apply_patch(patch):
        source = _get_projection(projections, root, operation.path)
        if source is None:
            continue

        if operation.kind == "add":
            source.projected_count = operation.added
        elif operation.kind == "delete":
            source.projected_count = 0
        else:
            source.projected_count = max(
                0,
                source.projected_count + operation.added - operation.removed,
            )
        source.text = None

        if operation.destination:
            destination = _get_projection(projections, root, operation.destination)
            if destination is not None:
                destination.projected_count = source.projected_count
                destination.text = None


def _parse_apply_patch(patch: str) -> list[_PatchOperation]:
    operations: list[_PatchOperation] = []
    current: _PatchOperation | None = None

    for line in patch.splitlines():
        for kind in ("Add", "Update", "Delete"):
            prefix = f"*** {kind} File: "
            if line.startswith(prefix):
                current = _PatchOperation(kind.lower(), line[len(prefix) :].strip())
                operations.append(current)
                break
        else:
            if current is None:
                continue
            if line.startswith("*** Move to: "):
                current.destination = line.removeprefix("*** Move to: ").strip()
            elif line.startswith("+") and not line.startswith("+++"):
                current.added += 1
            elif line.startswith("-") and not line.startswith("---"):
                current.removed += 1
    return operations


def _fallback_paths(
    tool_input: Mapping[str, Any],
    event_data: Mapping[str, Any] | None,
) -> list[str]:
    paths: list[str] = []
    canonical_paths = event_data.get("canonical_file_paths") if event_data else None
    if isinstance(canonical_paths, list):
        paths.extend(path for path in canonical_paths if isinstance(path, str))

    input_paths = tool_input.get("file_paths")
    if isinstance(input_paths, list):
        paths.extend(path for path in input_paths if isinstance(path, str))
    input_path = tool_input.get("file_path") or tool_input.get("path")
    if isinstance(input_path, str):
        paths.append(input_path)
    return list(dict.fromkeys(paths))


def _line_count(text: str | None) -> int:
    return len(text.splitlines()) if text else 0
