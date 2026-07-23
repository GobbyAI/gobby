"""Build shared audit reports from mypy diagnostics."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from gobby.test_quality.models import AuditIssue, AuditReport, AuditWarning
from gobby.test_types._mypy import run_mypy

_PYTHON_SUFFIXES = {".py", ".pyi"}


def audit_types_paths(
    paths: Iterable[str | Path],
    *,
    root: Path,
    mypy_command: str | None = None,
) -> AuditReport:
    """Audit Python files below paths and return a shared ratchet report."""

    root_resolved = root.resolve()
    targets = tuple(_resolve_target(path, root=root_resolved) for path in paths)
    report_paths = tuple(_relative_path(target, root=root_resolved) for target in targets)
    files = _discover_python_files(targets, root=root_resolved)
    if not files:
        return AuditReport(
            root=str(root_resolved),
            paths=report_paths,
            issues=(),
            files_scanned=0,
            tests_scanned=0,
            warnings=(
                AuditWarning(
                    code="NO_ANALYZABLE_FILES",
                    message="No Python files were found under the requested paths",
                ),
            ),
        )

    diagnostics = run_mypy(report_paths, root=root_resolved, mypy_command=mypy_command)
    requested_diagnostics = tuple(
        diagnostic
        for diagnostic in diagnostics
        if _is_requested_path(root_resolved / diagnostic.path, targets)
    )
    issues = tuple(
        AuditIssue(
            path=diagnostic.path,
            test_name="",
            issue_code=diagnostic.code,
            severity="high",
            line=diagnostic.line,
            message=diagnostic.message,
        )
        for diagnostic in requested_diagnostics
    )
    return AuditReport(
        root=str(root_resolved),
        paths=report_paths,
        issues=issues,
        files_scanned=len(files),
        tests_scanned=0,
    )


def _resolve_target(path: str | Path, *, root: Path) -> Path:
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"requested path is outside the project root: {path}") from exc
    return resolved


def _relative_path(path: Path, *, root: Path) -> str:
    relative = path.relative_to(root)
    return "." if not relative.parts else relative.as_posix()


def _discover_python_files(targets: tuple[Path, ...], *, root: Path) -> tuple[Path, ...]:
    files: set[Path] = set()
    for target in targets:
        candidates = (target,) if target.is_file() else target.rglob("*") if target.is_dir() else ()
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix not in _PYTHON_SUFFIXES:
                continue
            resolved = candidate.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            files.add(resolved)
    return tuple(sorted(files))


def _is_requested_path(path: Path, targets: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    for target in targets:
        if target.is_file() and resolved == target:
            return True
        if target.is_dir():
            try:
                resolved.relative_to(target)
            except ValueError:
                continue
            return True
    return False
