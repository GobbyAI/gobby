"""Durable, symbol-scoped validation for plan Targets blocks."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

import psycopg

from gobby.code_index.models import CODE_INDEX_UUID_NAMESPACE
from gobby.plans.parser import Kind, PlanDocument
from gobby.plans.semantic_lint import has_generated_header, iter_target_block_lines

INDEX_UNAVAILABLE = "symbol_index_unavailable"
INDEX_STALE = "symbol_index_stale"
MISSING_SYMBOL_SCOPE = "target_symbol_required"
UNRESOLVED_SYMBOL = "target_symbol_unresolved"
AMBIGUOUS_SYMBOL = "target_symbol_ambiguous"
INVALID_WILDCARD_REASON = "target_wildcard_reason_invalid"
UUID_REFERENCE = "target_uuid_forbidden"
SCOPE_CONFLICT = "target_scope_conflict"
MALFORMED_REFERENCE = "target_reference_malformed"
CONSUMER_COVERAGE = "consumer-coverage"

SYMBOL_TARGET_ISSUE_CODES = frozenset(
    {
        INDEX_UNAVAILABLE,
        INDEX_STALE,
        MISSING_SYMBOL_SCOPE,
        UNRESOLVED_SYMBOL,
        AMBIGUOUS_SYMBOL,
        INVALID_WILDCARD_REASON,
        UUID_REFERENCE,
        SCOPE_CONFLICT,
        MALFORMED_REFERENCE,
        CONSUMER_COVERAGE,
    }
)

_UUID_RE = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_SCOPE_REASON_RE = re.compile(
    r"\s*(?:—|–|-)\s*scope-reason\s*:\s*(?P<reason>.*)\s*$",
    re.IGNORECASE,
)


class SymbolIndexStorage(Protocol):
    """Storage calls required by symbol target validation."""

    def get_project_stats(self, project_id: str) -> Any | None: ...

    def get_file(self, project_id: str, file_path: str) -> Any | None: ...

    def get_symbols_for_file(self, project_id: str, file_path: str) -> list[Any]: ...

    def get_symbol_usages(self, project_id: str, symbol_id: str) -> list[str]: ...


@dataclass(frozen=True)
class SymbolValidationScope:
    """Resolved filesystem and code-index scope for symbol validation."""

    filesystem_root: Path
    primary_project_id: str
    parent_project_id: str | None = None

    @property
    def project_ids(self) -> tuple[str, ...]:
        if self.parent_project_id is None:
            return (self.primary_project_id,)
        return (self.primary_project_id, self.parent_project_id)


@dataclass(frozen=True)
class SymbolTarget:
    """One parsed target reference from a deliverable Targets block."""

    section_id: str
    file_path: str
    symbol: str | None
    wildcard: bool
    scope_reason: str | None
    raw: str

    @property
    def reference(self) -> str:
        if self.wildcard:
            return f"{self.file_path}::*"
        if self.symbol is not None:
            return f"{self.file_path}::{self.symbol}"
        return self.file_path


@dataclass(frozen=True)
class SymbolValidationIssue:
    """Structured, stable diagnostic for a target or index failure."""

    code: str
    message: str
    section_id: str | None = None
    file_path: str | None = None
    symbol: str | None = None
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
        }
        if self.section_id is not None:
            result["section_id"] = self.section_id
        if self.file_path is not None:
            result["file_path"] = self.file_path
        if self.symbol is not None:
            result["symbol"] = self.symbol
        return result


@dataclass(frozen=True)
class SymbolValidationResult:
    """Symbol validation envelope embedded in plan validation results."""

    status: Literal["passed", "failed", "skipped"]
    checked_targets: tuple[str, ...] = ()
    checked_symbols: tuple[str, ...] = ()
    issues: tuple[SymbolValidationIssue, ...] = ()

    @property
    def errors(self) -> list[str]:
        return [issue.message for issue in self.issues if issue.blocking]

    @property
    def warnings(self) -> list[str]:
        return [issue.message for issue in self.issues if not issue.blocking]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_targets": list(self.checked_targets),
            "checked_symbols": list(self.checked_symbols),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def skipped_symbol_validation() -> SymbolValidationResult:
    """Return the empty envelope used when structural validation cannot proceed."""

    return SymbolValidationResult(status="skipped")


def parse_symbol_targets(
    plan_doc: PlanDocument,
) -> tuple[tuple[SymbolTarget, ...], tuple[SymbolValidationIssue, ...]]:
    """Parse canonical target references from every deliverable section."""

    targets: list[SymbolTarget] = []
    issues: list[SymbolValidationIssue] = []
    for section in plan_doc.sections:
        if section.kind is not Kind.deliverable:
            continue
        for line in iter_target_block_lines(plan_doc, section):
            parsed, line_issues = _parse_target_line(line, section.section_id)
            targets.extend(parsed)
            issues.extend(line_issues)

    by_file: dict[str, list[SymbolTarget]] = {}
    for target in targets:
        by_file.setdefault(target.file_path, []).append(target)
    for file_path, file_targets in by_file.items():
        has_wildcard = any(target.wildcard for target in file_targets)
        has_exact = any(target.symbol is not None for target in file_targets)
        if has_wildcard and has_exact:
            issues.append(
                SymbolValidationIssue(
                    code=SCOPE_CONFLICT,
                    message=(
                        f"Targets for {file_path} mix exact symbols with `::*`; "
                        "choose one scope form for the file"
                    ),
                    file_path=file_path,
                )
            )
    return tuple(targets), tuple(issues)


def validate_symbol_targets(
    plan_doc: PlanDocument,
    *,
    project_context: Mapping[str, Any] | None,
    expected_project_id: str | None = None,
    code_index: Any | None,
    required: bool,
    consumer_coverage_blocking: bool = False,
) -> SymbolValidationResult:
    """Validate parsed targets against a fresh project code index."""

    targets, parse_issues = parse_symbol_targets(plan_doc)
    has_exact_targets = any(target.symbol is not None for target in targets)

    scope, scope_error = _resolve_validation_scope(
        project_context,
        expected_project_id=expected_project_id,
    )
    unavailable = _unavailable_context_issue(
        scope=scope,
        scope_error=scope_error,
        code_index=code_index,
        required=required,
    )
    if unavailable is not None:
        issues = [unavailable]
        if has_exact_targets:
            issues.append(_consumer_coverage_skipped("project code index is unavailable"))
        return SymbolValidationResult(
            status="failed" if required else "skipped",
            issues=tuple(issues),
        )

    storage = _index_storage(code_index)
    assert scope is not None
    primary_project_stats: Any | None = None
    for project_id in scope.project_ids:
        try:
            project_stats = storage.get_project_stats(project_id)
        except psycopg.Error as exc:
            return _index_read_failure(
                required,
                f"Code index is unavailable: {exc}",
                consumer_coverage=has_exact_targets,
            )
        if project_stats is None:
            return _index_read_failure(
                required,
                f"No code index is available for project {project_id}",
                consumer_coverage=has_exact_targets,
            )
        if project_id == scope.primary_project_id:
            primary_project_stats = project_stats
    assert primary_project_stats is not None

    issues = list(parse_issues)
    checked_targets = _unique(target.reference for target in targets)
    checked_symbols = _unique(target.reference for target in targets if target.symbol is not None)

    by_file: dict[str, list[SymbolTarget]] = {}
    for target in targets:
        by_file.setdefault(target.file_path, []).append(target)
    for file_path, file_targets in by_file.items():
        issues.extend(
            _validate_file_targets(
                storage,
                scope=scope,
                file_path=file_path,
                targets=file_targets,
            )
        )

    if has_exact_targets:
        skip_reason = _consumer_coverage_skip_reason(
            primary_project_stats,
            scope.filesystem_root,
            issues,
        )
        if skip_reason is not None:
            issues.append(_consumer_coverage_skipped(skip_reason))
        else:
            issues.extend(
                _validate_consumer_coverage(
                    storage,
                    scope=scope,
                    targets=targets,
                    blocking=consumer_coverage_blocking,
                )
            )

    if not required and any(issue.code in {INDEX_UNAVAILABLE, INDEX_STALE} for issue in issues):
        return SymbolValidationResult(
            status="skipped",
            checked_targets=checked_targets,
            checked_symbols=checked_symbols,
            issues=tuple(
                SymbolValidationIssue(
                    code=issue.code,
                    message=issue.message,
                    section_id=issue.section_id,
                    file_path=issue.file_path,
                    symbol=issue.symbol,
                    blocking=False,
                )
                for issue in issues
            ),
        )

    return SymbolValidationResult(
        status="failed" if any(issue.blocking for issue in issues) else "passed",
        checked_targets=checked_targets,
        checked_symbols=checked_symbols,
        issues=tuple(issues),
    )


def _parse_target_line(
    line: str,
    section_id: str,
) -> tuple[list[SymbolTarget], list[SymbolValidationIssue]]:
    reason_match = _SCOPE_REASON_RE.search(line)
    if reason_match is not None:
        target_text = line[: reason_match.start()]
        reason_suffix = line[reason_match.start() :].strip()
    else:
        target_text = line
        reason_suffix = ""

    matches = list(_BACKTICK_RE.finditer(target_text))
    if matches:
        tokens = [match.group(1).strip() for match in matches]
        trailing = reason_suffix or target_text[matches[-1].end() :].strip()
    else:
        stripped = _BULLET_RE.sub("", target_text, count=1).strip()
        tokens = [token.strip() for token in stripped.split(",")]
        trailing = reason_suffix

    targets: list[SymbolTarget] = []
    issues: list[SymbolValidationIssue] = []
    for index, token in enumerate(tokens):
        token_trailing = trailing if index == len(tokens) - 1 else ""
        target, issue = _parse_target_token(token, token_trailing, section_id)
        if target is not None:
            targets.append(target)
        if issue is not None:
            issues.append(issue)
    return targets, issues


def _parse_target_token(
    token: str,
    trailing: str,
    section_id: str,
) -> tuple[SymbolTarget | None, SymbolValidationIssue | None]:
    raw = token
    reason: str | None = None
    reason_match = _SCOPE_REASON_RE.search(token)
    if reason_match is not None:
        reason = reason_match.group("reason").strip()
        token = token[: reason_match.start()].strip()
    elif trailing:
        trailing_match = _SCOPE_REASON_RE.fullmatch(trailing)
        if trailing_match is not None:
            reason = trailing_match.group("reason").strip()

    if _UUID_RE.fullmatch(token):
        return None, _target_issue(
            UUID_REFERENCE,
            "Targets must use durable qualified names instead of symbol UUIDs",
            section_id,
            raw,
        )

    file_part, separator, symbol = token.partition("::")
    file_path = _normalize_target_path(file_part)
    if file_path is None or (separator and not symbol):
        return None, _target_issue(
            MALFORMED_REFERENCE,
            f"Malformed target reference `{raw}`",
            section_id,
            raw,
        )
    if symbol and _UUID_RE.fullmatch(symbol):
        return None, SymbolValidationIssue(
            code=UUID_REFERENCE,
            message=(
                f"Target `{raw}` uses an unstable symbol UUID; use the indexed qualified_name"
            ),
            section_id=section_id,
            file_path=file_path,
            symbol=symbol,
        )
    if symbol.startswith(":") or any(character.isspace() for character in symbol):
        return None, _target_issue(
            MALFORMED_REFERENCE,
            f"Malformed target reference `{raw}`",
            section_id,
            raw,
        )

    wildcard = symbol == "*"
    target = SymbolTarget(
        section_id=section_id,
        file_path=file_path,
        symbol=None if wildcard or not separator else symbol,
        wildcard=wildcard,
        scope_reason=reason,
        raw=raw,
    )
    if wildcard and not reason:
        return target, SymbolValidationIssue(
            code=INVALID_WILDCARD_REASON,
            message=(
                f"Wildcard target `{target.reference}` requires "
                "`scope-reason: <non-empty explanation>`"
            ),
            section_id=section_id,
            file_path=file_path,
        )
    if reason is not None and not wildcard:
        return target, _target_issue(
            MALFORMED_REFERENCE,
            f"`scope-reason` is only valid for `::*` targets: `{raw}`",
            section_id,
            raw,
        )
    return target, None


def _normalize_target_path(value: str) -> str | None:
    candidate = value.strip()
    if (
        not candidate
        or candidate.endswith("/")
        or any(character.isspace() for character in candidate)
        or ":" in candidate
        or "\\" in candidate
    ):
        return None
    while candidate.startswith("./"):
        candidate = candidate[2:]
    path = PurePosixPath(candidate)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _validate_file_targets(
    storage: SymbolIndexStorage,
    *,
    scope: SymbolValidationScope,
    file_path: str,
    targets: list[SymbolTarget],
) -> list[SymbolValidationIssue]:
    try:
        selected_project_id, indexed_file = _visible_indexed_file(storage, scope, file_path)
    except psycopg.Error as exc:
        return [
            SymbolValidationIssue(
                code=INDEX_UNAVAILABLE,
                message=f"Could not read code index record for {file_path}: {exc}",
                file_path=file_path,
            )
        ]
    if indexed_file is None:
        return _validate_unindexed_targets(targets)

    current_path = scope.filesystem_root / file_path
    current_hash = _file_sha256(current_path)
    if current_hash is None or current_hash != str(indexed_file.content_hash):
        return [
            SymbolValidationIssue(
                code=INDEX_STALE,
                message=(
                    f"Code index is stale for {file_path}; refresh it before "
                    "validating symbol targets"
                ),
                file_path=file_path,
            )
        ]

    try:
        symbols = storage.get_symbols_for_file(selected_project_id, file_path)
    except psycopg.Error as exc:
        return [
            SymbolValidationIssue(
                code=INDEX_UNAVAILABLE,
                message=f"Could not read indexed symbols for {file_path}: {exc}",
                file_path=file_path,
            )
        ]
    if int(indexed_file.symbol_count) > 0 and not symbols:
        return [
            SymbolValidationIssue(
                code=INDEX_UNAVAILABLE,
                message=(
                    f"Code index record for {file_path} reports symbols but none are available"
                ),
                file_path=file_path,
            )
        ]

    issues: list[SymbolValidationIssue] = []
    qualified_names: dict[str, int] = {}
    for indexed_symbol in symbols:
        qualified_name = str(indexed_symbol.qualified_name)
        qualified_names[qualified_name] = qualified_names.get(qualified_name, 0) + 1
    for target in targets:
        if target.wildcard:
            continue
        if target.symbol is None:
            if symbols:
                issues.append(
                    SymbolValidationIssue(
                        code=MISSING_SYMBOL_SCOPE,
                        message=(
                            f"Target `{target.file_path}` contains indexed symbols; "
                            "name exact qualified symbols or use a justified `::*` scope"
                        ),
                        section_id=target.section_id,
                        file_path=target.file_path,
                    )
                )
            continue
        match_count = qualified_names.get(target.symbol, 0)
        if match_count == 0:
            issues.append(
                SymbolValidationIssue(
                    code=UNRESOLVED_SYMBOL,
                    message=(
                        f"Target `{target.reference}` does not match an indexed "
                        "qualified_name in that file"
                    ),
                    section_id=target.section_id,
                    file_path=target.file_path,
                    symbol=target.symbol,
                )
            )
        elif match_count > 1:
            issues.append(
                SymbolValidationIssue(
                    code=AMBIGUOUS_SYMBOL,
                    message=(
                        f"Target `{target.reference}` matches {match_count} indexed "
                        "symbols in that file"
                    ),
                    section_id=target.section_id,
                    file_path=target.file_path,
                    symbol=target.symbol,
                )
            )
    return issues


def _validate_unindexed_targets(targets: list[SymbolTarget]) -> list[SymbolValidationIssue]:
    issues: list[SymbolValidationIssue] = []
    for target in targets:
        if target.symbol is not None or target.wildcard:
            issues.append(
                SymbolValidationIssue(
                    code=UNRESOLVED_SYMBOL,
                    message=(
                        f"Target `{target.reference}` names symbol scope for a file "
                        "with no index record"
                    ),
                    section_id=target.section_id,
                    file_path=target.file_path,
                    symbol=target.symbol,
                )
            )
    return issues


def _validate_consumer_coverage(
    storage: SymbolIndexStorage,
    *,
    scope: SymbolValidationScope,
    targets: tuple[SymbolTarget, ...],
    blocking: bool,
) -> list[SymbolValidationIssue]:
    targeted_files = {target.file_path for target in targets}
    exact_targets = [target for target in targets if target.symbol is not None]
    usage_cache: dict[tuple[str, str], list[str]] = {}
    issues: list[SymbolValidationIssue] = []
    for target in exact_targets:
        try:
            selected_project_id, indexed_file = _visible_indexed_file(
                storage,
                scope,
                target.file_path,
            )
            if indexed_file is None:
                continue
            symbols = storage.get_symbols_for_file(selected_project_id, target.file_path)
        except psycopg.Error:
            return [_consumer_coverage_skipped("code index usages are unavailable")]
        matches = [
            symbol
            for symbol in symbols
            if str(symbol.qualified_name) == target.symbol and getattr(symbol, "id", None)
        ]
        if len(matches) != 1:
            continue
        symbol_id = str(matches[0].id)
        usage_key = (selected_project_id, symbol_id)
        if usage_key not in usage_cache:
            try:
                usage_cache[usage_key] = storage.get_symbol_usages(
                    selected_project_id,
                    symbol_id,
                )
            except (AttributeError, psycopg.Error):
                return [_consumer_coverage_skipped("code index usages are unavailable")]
        missing = sorted(
            {
                consumer
                for consumer in usage_cache[usage_key]
                if consumer not in targeted_files
                and _is_owned_consumer(scope.filesystem_root, consumer)
            }
        )
        if not missing:
            continue
        issues.append(
            SymbolValidationIssue(
                code=CONSUMER_COVERAGE,
                message=(
                    f"consumer-coverage: section {target.section_id}: symbol "
                    f"`{target.reference}` has consumers missing from Targets: "
                    f"{', '.join(missing)}"
                ),
                section_id=target.section_id,
                file_path=target.file_path,
                symbol=target.symbol,
                blocking=blocking,
            )
        )
    return issues


def _consumer_coverage_skip_reason(
    project_stats: Any,
    project_root: Path,
    issues: list[SymbolValidationIssue],
) -> str | None:
    if any(issue.code in {INDEX_UNAVAILABLE, INDEX_STALE} for issue in issues):
        return "code index does not cover the plan checkout"
    indexed_root = getattr(project_stats, "root_path", None)
    if not isinstance(indexed_root, str) or not indexed_root:
        return None
    try:
        roots_match = Path(indexed_root).resolve() == project_root.resolve()
    except OSError:
        roots_match = False
    if roots_match:
        return None
    if (project_root / ".git").is_file():
        return "code index does not cover this worktree checkout (#20664)"
    return f"code index covers {indexed_root}, not plan checkout {project_root}"


def _consumer_coverage_skipped(reason: str) -> SymbolValidationIssue:
    return SymbolValidationIssue(
        code=CONSUMER_COVERAGE,
        message=f"consumer-coverage skipped: {reason}",
        blocking=False,
    )


def _is_owned_consumer(project_root: Path, file_path: str) -> bool:
    candidate = PurePosixPath(file_path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        return False
    if any(part in {"node_modules", "vendor"} for part in candidate.parts):
        return False
    root = project_root.resolve()
    source_path = (project_root / candidate.as_posix()).resolve()
    try:
        source_path.relative_to(root)
    except ValueError:
        return False
    return source_path.is_file() and not has_generated_header(source_path)


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as file_handle:
            for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _index_storage(code_index: Any) -> SymbolIndexStorage:
    storage = getattr(code_index, "storage", code_index)
    return cast(SymbolIndexStorage, storage)


def _visible_indexed_file(
    storage: SymbolIndexStorage,
    scope: SymbolValidationScope,
    file_path: str,
) -> tuple[str, Any | None]:
    selected_project_id = scope.primary_project_id
    indexed_file = storage.get_file(selected_project_id, file_path)
    if indexed_file is None and scope.parent_project_id is not None:
        selected_project_id = scope.parent_project_id
        indexed_file = storage.get_file(selected_project_id, file_path)
    return selected_project_id, indexed_file


def _resolve_validation_scope(
    project_context: Mapping[str, Any] | None,
    *,
    expected_project_id: str | None,
) -> tuple[SymbolValidationScope | None, str | None]:
    if project_context is None:
        return None, "project context is missing"

    project_id = _nonempty_context_string(project_context.get("id"))
    project_path = _nonempty_context_string(project_context.get("project_path"))
    if project_id is None or project_path is None:
        return None, "project context requires id and project_path"

    parent_id_value = project_context.get("parent_project_id")
    parent_path_value = project_context.get("parent_project_path")
    has_parent_id = parent_id_value is not None
    has_parent_path = parent_path_value is not None
    if has_parent_id != has_parent_path:
        return None, "isolation context requires parent_project_id and parent_project_path together"

    filesystem_root = _canonical_root(Path(project_path))
    logical_project_id = project_id
    primary_project_id = project_id
    parent_project_id: str | None = None
    if has_parent_id:
        parent_project_id = _nonempty_context_string(parent_id_value)
        parent_project_path = _nonempty_context_string(parent_path_value)
        if parent_project_id is None or parent_project_path is None:
            return None, "isolation parent fields must be non-empty strings"
        if project_id != parent_project_id:
            return None, "isolation context id does not match parent_project_id"
        logical_project_id = parent_project_id
        primary_project_id = str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, str(filesystem_root)))

    if expected_project_id is not None and expected_project_id != logical_project_id:
        return (
            None,
            f"project context does not match the requested logical project {expected_project_id}",
        )
    return (
        SymbolValidationScope(
            filesystem_root=filesystem_root,
            primary_project_id=primary_project_id,
            parent_project_id=parent_project_id,
        ),
        None,
    )


def _nonempty_context_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _canonical_root(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _unavailable_context_issue(
    *,
    scope: SymbolValidationScope | None,
    scope_error: str | None,
    code_index: Any | None,
    required: bool,
) -> SymbolValidationIssue | None:
    missing: list[str] = []
    if scope is None:
        missing.append(scope_error or "project scope")
    if code_index is None:
        missing.append("code index")
    if not missing:
        return None
    prefix = "Symbol validation cannot run" if required else "Symbol validation skipped"
    return SymbolValidationIssue(
        code=INDEX_UNAVAILABLE,
        message=f"{prefix}: unavailable {', '.join(missing)}",
        blocking=required,
    )


def _index_read_failure(
    required: bool,
    message: str,
    *,
    consumer_coverage: bool = False,
) -> SymbolValidationResult:
    issues = [
        SymbolValidationIssue(
            code=INDEX_UNAVAILABLE,
            message=message,
            blocking=required,
        )
    ]
    if consumer_coverage:
        issues.append(_consumer_coverage_skipped("project code index is unavailable"))
    return SymbolValidationResult(
        status="failed" if required else "skipped",
        issues=tuple(issues),
    )


def _target_issue(
    code: str,
    message: str,
    section_id: str,
    raw: str,
) -> SymbolValidationIssue:
    return SymbolValidationIssue(
        code=code,
        message=message,
        section_id=section_id,
        symbol=raw,
    )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
