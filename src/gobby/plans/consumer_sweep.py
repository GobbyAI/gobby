"""Code-index-backed consumer sweep for plan deliverable targets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from gobby.plans.parser import Kind, PlanDocument, PlanSection
from gobby.plans.semantic_lint import (
    collect_target_inventory,
    find_file_paths_in_text,
    normalize_file_path,
    section_body_lines,
)

_SYMBOL_REF_RE = re.compile(
    r"\bsymbol\s*:\s*(?:`(?P<ticked>[A-Za-z_][\w.]+)`|(?P<bare>[A-Za-z_][\w.]+))",
    re.IGNORECASE,
)
_BACKTICK_SYMBOL_RE = re.compile(r"`(?P<symbol>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)`")
_CHANGE_VERBS_RE = re.compile(
    r"\b(remove|delete|rename|move|change|modify|update|refactor|rewrite|replace|extract|alter)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ConsumerSweepIssue:
    """One direct consumer missing from a deliverable's target inventory."""

    code: str
    section_id: str
    message: str
    missing_consumers: tuple[str, ...]
    trigger: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_error(self) -> str:
        return f"{self.code}: section {self.section_id}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "section_id": self.section_id,
            "message": self.message,
            "missing_consumers": list(self.missing_consumers),
            "trigger": self.trigger,
        }
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class ConsumerSweepResult:
    """Consumer sweep result."""

    issues: tuple[ConsumerSweepIssue, ...] = ()
    skipped: bool = False
    skip_reason: str | None = None

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def errors(self) -> list[str]:
        return [issue.to_error() for issue in self.issues]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "valid": self.valid,
            "skipped": self.skipped,
            "issues": [issue.to_dict() for issue in self.issues],
        }
        if self.skip_reason:
            payload["skip_reason"] = self.skip_reason
        return payload


def run_consumer_sweep(
    plan_doc: PlanDocument,
    *,
    project_id: str | None,
    code_index: Any | None,
) -> ConsumerSweepResult:
    """Fail when direct code-index consumers are missing from section targets."""
    if not project_id:
        return ConsumerSweepResult(skipped=True, skip_reason="missing project_id")

    storage = _code_index_storage(code_index)
    if storage is None:
        return ConsumerSweepResult(skipped=True, skip_reason="code index storage unavailable")
    if not _project_is_indexed(storage, project_id):
        return ConsumerSweepResult(skipped=True, skip_reason="project is not indexed")

    issues: list[ConsumerSweepIssue] = []
    for section in plan_doc.sections:
        if section.kind is not Kind.deliverable:
            continue
        targets = collect_target_inventory(plan_doc, section)
        if not targets:
            continue
        issues.extend(_sweep_section(plan_doc, section, project_id, storage, targets))
    return ConsumerSweepResult(tuple(issues))


def _sweep_section(
    plan_doc: PlanDocument,
    section: PlanSection,
    project_id: str,
    storage: Any,
    targets: frozenset[str],
) -> list[ConsumerSweepIssue]:
    issues: list[ConsumerSweepIssue] = []
    text = _section_text(plan_doc, section)

    for symbol_ref in sorted(_symbol_intents(text)):
        symbols = _resolve_symbols(storage, project_id, symbol_ref)
        if not symbols:
            continue
        symbol_ids = tuple(_symbol_attr(symbol, "id") for symbol in symbols)
        names = tuple({symbol_ref, symbol_ref.rsplit(".", 1)[-1]})
        consumers = _direct_symbol_consumers(storage, project_id, symbol_ids, names)
        missing = tuple(sorted(path for path in consumers if path not in targets))
        if missing:
            issues.append(
                ConsumerSweepIssue(
                    code="consumer-sweep",
                    section_id=section.section_id,
                    trigger=f"symbol:{symbol_ref}",
                    missing_consumers=missing,
                    message=(
                        f"direct consumer files for symbol {symbol_ref} are missing from "
                        f"Target/Targets: {', '.join(missing)}"
                    ),
                    details={"targets": sorted(targets)},
                )
            )

    for file_path in sorted(_file_change_intents(plan_doc, section)):
        consumers = _direct_file_consumers(storage, project_id, file_path)
        missing = tuple(sorted(path for path in consumers if path not in targets))
        if missing:
            issues.append(
                ConsumerSweepIssue(
                    code="consumer-sweep",
                    section_id=section.section_id,
                    trigger=f"file:{file_path}",
                    missing_consumers=missing,
                    message=(
                        f"direct consumer files for {file_path} are missing from "
                        f"Target/Targets: {', '.join(missing)}"
                    ),
                    details={"targets": sorted(targets)},
                )
            )
    return issues


def _section_text(plan_doc: PlanDocument, section: PlanSection) -> str:
    lines = section_body_lines(plan_doc, section, before_acceptance=False)
    prose = [*lines, *(item.prose for item in section.acceptance_items)]
    return "\n".join(prose)


def _symbol_intents(text: str) -> set[str]:
    refs: set[str] = set()
    for match in _SYMBOL_REF_RE.finditer(text):
        refs.add(match.group("ticked") or match.group("bare") or "")
    for match in _BACKTICK_SYMBOL_RE.finditer(text):
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 80)
        if _CHANGE_VERBS_RE.search(text[start:end]):
            refs.add(match.group("symbol"))
    return {ref for ref in refs if ref}


def _file_change_intents(plan_doc: PlanDocument, section: PlanSection) -> set[str]:
    paths: set[str] = set()
    for line in section_body_lines(plan_doc, section, before_acceptance=False):
        if not _CHANGE_VERBS_RE.search(line):
            continue
        paths.update(find_file_paths_in_text(line))
    for item in section.acceptance_items:
        if not _CHANGE_VERBS_RE.search(item.prose):
            continue
        paths.update(find_file_paths_in_text(item.prose))
    return paths


def _resolve_symbols(storage: Any, project_id: str, symbol_ref: str) -> tuple[Any, ...]:
    search = getattr(storage, "search_symbols_by_name", None)
    if not callable(search):
        return ()
    symbols = tuple(search(symbol_ref, project_id, limit=20))
    exact_qualified = tuple(
        symbol for symbol in symbols if _symbol_attr(symbol, "qualified_name") == symbol_ref
    )
    if exact_qualified:
        return exact_qualified
    leaf = symbol_ref.rsplit(".", 1)[-1]
    exact_names = tuple(symbol for symbol in symbols if _symbol_attr(symbol, "name") == leaf)
    if len(exact_names) == 1:
        return exact_names
    return ()


def _direct_symbol_consumers(
    storage: Any,
    project_id: str,
    symbol_ids: tuple[str, ...],
    callee_names: tuple[str, ...],
) -> set[str]:
    fake = getattr(storage, "find_direct_callers", None)
    if callable(fake):
        return _row_paths(fake(project_id, symbol_ids, callee_names))

    db = getattr(storage, "db", None)
    if db is None or not hasattr(db, "fetchall"):
        return set()

    conditions: list[str] = []
    params: list[Any] = [project_id]
    if symbol_ids:
        placeholders = ", ".join("?" for _ in symbol_ids)
        conditions.append(f"callee_symbol_id IN ({placeholders})")
        params.extend(symbol_ids)
    if callee_names:
        placeholders = ", ".join("?" for _ in callee_names)
        conditions.append(f"callee_name IN ({placeholders})")
        params.extend(callee_names)
    if not conditions:
        return set()

    rows = db.fetchall(
        "SELECT DISTINCT file_path FROM code_calls "
        "WHERE project_id = ? AND (" + " OR ".join(conditions) + ")",
        tuple(params),
    )
    return _row_paths(rows)


def _direct_file_consumers(storage: Any, project_id: str, file_path: str) -> set[str]:
    symbols = _symbols_for_file(storage, project_id, file_path)
    symbol_ids = tuple(_symbol_attr(symbol, "id") for symbol in symbols)
    module_candidates = tuple(_module_candidates(file_path))

    fake = getattr(storage, "find_direct_file_consumers", None)
    if callable(fake):
        return _row_paths(fake(project_id, file_path, module_candidates, symbol_ids))

    consumers = _direct_symbol_consumers(storage, project_id, symbol_ids, ())
    db = getattr(storage, "db", None)
    if db is None or not hasattr(db, "fetchall") or not module_candidates:
        return consumers

    placeholders = ", ".join("?" for _ in module_candidates)
    rows = db.fetchall(
        "SELECT DISTINCT source_file AS file_path FROM code_imports "
        f"WHERE project_id = ? AND target_module IN ({placeholders})",
        (project_id, *module_candidates),
    )
    consumers.update(_row_paths(rows))
    consumers.discard(file_path)
    return consumers


def _symbols_for_file(storage: Any, project_id: str, file_path: str) -> tuple[Any, ...]:
    get_symbols = getattr(storage, "get_symbols_for_file", None)
    if not callable(get_symbols):
        return ()
    return tuple(get_symbols(project_id, file_path))


def _module_candidates(file_path: str) -> set[str]:
    normalized = normalize_file_path(file_path)
    if normalized is None or not normalized.endswith(".py"):
        return set()
    without_suffix = normalized[:-3]
    if without_suffix.startswith("src/"):
        without_suffix = without_suffix[4:]
    if without_suffix.endswith("/__init__"):
        without_suffix = without_suffix[: -len("/__init__")]
    module = without_suffix.replace("/", ".")
    candidates = {module}
    parts = module.split(".")
    if len(parts) > 1:
        candidates.add(".".join(parts[:-1]))
    return {candidate for candidate in candidates if candidate}


def _code_index_storage(code_index: Any | None) -> Any | None:
    if code_index is None:
        return None
    storage = getattr(code_index, "storage", None)
    if storage is None:
        return None
    return storage() if callable(storage) else storage


def _project_is_indexed(storage: Any, project_id: str) -> bool:
    stats = getattr(storage, "get_project_stats", None)
    if callable(stats):
        return stats(project_id) is not None
    count_files = getattr(storage, "count_files", None)
    if callable(count_files):
        return bool(count_files(project_id))
    return True


def _row_paths(rows: Any) -> set[str]:
    paths: set[str] = set()
    for row in rows or ():
        value = _row_value(row, "file_path") or _row_value(row, "source_file")
        normalized = normalize_file_path(str(value)) if value else None
        if normalized is not None:
            paths.add(normalized)
    return paths


def _row_value(row: Any, key: str) -> Any | None:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def _symbol_attr(symbol: Any, attr: str) -> str:
    value = getattr(symbol, attr, None)
    if value is None and isinstance(symbol, dict):
        value = symbol.get(attr)
    return str(value or "")
