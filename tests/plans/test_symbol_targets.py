from __future__ import annotations

import hashlib
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from gobby.code_index.models import CODE_INDEX_UUID_NAMESPACE
from gobby.plans.parser import PlanDocument, parse_plan
from gobby.plans.symbol_targets import (
    AMBIGUOUS_SYMBOL,
    INDEX_STALE,
    INDEX_UNAVAILABLE,
    INVALID_WILDCARD_REASON,
    MALFORMED_REFERENCE,
    MISSING_SYMBOL_SCOPE,
    SCOPE_CONFLICT,
    SYMBOL_TARGET_ISSUE_CODES,
    UNRESOLVED_SYMBOL,
    UUID_REFERENCE,
    SymbolValidationResult,
    parse_symbol_targets,
    validate_symbol_targets,
)

PROJECT_ID = "project-1"


@dataclass(frozen=True)
class _IndexedFile:
    content_hash: str
    symbol_count: int
    language: str = "python"


@dataclass(frozen=True)
class _Symbol:
    qualified_name: str
    id: str = "symbol-id"


class _Index:
    def __init__(
        self,
        *,
        files: dict[str, _IndexedFile] | None = None,
        symbols: dict[str, list[_Symbol]] | None = None,
        available: bool = True,
        project_files: dict[str, dict[str, _IndexedFile]] | None = None,
        project_symbols: dict[str, dict[str, list[_Symbol]]] | None = None,
        available_projects: set[str] | None = None,
    ) -> None:
        self.files = {
            (project_id, file_path): indexed_file
            for project_id, project in (project_files or {}).items()
            for file_path, indexed_file in project.items()
        }
        self.files.update(
            {(PROJECT_ID, path): indexed_file for path, indexed_file in (files or {}).items()}
        )
        self.symbols = {
            (project_id, file_path): indexed_symbols
            for project_id, project in (project_symbols or {}).items()
            for file_path, indexed_symbols in project.items()
        }
        self.symbols.update(
            ((PROJECT_ID, path), indexed_symbols)
            for path, indexed_symbols in (symbols or {}).items()
        )
        self.available_projects = (
            available_projects
            if available_projects is not None
            else ({PROJECT_ID} if available else set())
        )
        self.file_queries: list[tuple[str, str]] = []
        self.symbol_queries: list[tuple[str, str]] = []

    def get_project_stats(self, project_id: str) -> object | None:
        return object() if project_id in self.available_projects else None

    def get_file(self, project_id: str, file_path: str) -> _IndexedFile | None:
        self.file_queries.append((project_id, file_path))
        return self.files.get((project_id, file_path))

    def get_symbols_for_file(self, project_id: str, file_path: str) -> list[_Symbol]:
        self.symbol_queries.append((project_id, file_path))
        return self.symbols.get((project_id, file_path), [])

    def get_symbol_usages(self, project_id: str, symbol_id: str) -> list[str]:
        return []


def _write_plan(tmp_path: Path, targets: str) -> Path:
    plan_path = tmp_path / "plan.md"
    header = textwrap.dedent(
        """
        > **Plan ID:** symbol-targets

        # Symbol Targets

        ## P1: Work
        `kind: framing`

        ### 1.1 Implement [category: code]
        `kind: deliverable`

        Targets:
        """
    ).lstrip()
    body = textwrap.dedent(targets).strip()
    plan_path.write_text(
        header
        + body
        + textwrap.dedent(
            """

            Implement the scoped changes.

            **Acceptance:**
            - 1.1.1 - Scoped changes pass focused tests. file: `src/acceptance.py`
            """
        ),
        encoding="utf-8",
    )
    return plan_path


def _parse(tmp_path: Path, targets: str) -> PlanDocument:
    return parse_plan(_write_plan(tmp_path, targets), parse_mode="draft")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _issue_codes(result: SymbolValidationResult) -> set[str]:
    return {issue.code for issue in result.issues}


def _project_context(root: Path) -> dict[str, str]:
    return {"id": PROJECT_ID, "project_path": str(root)}


def _overlay_context(root: Path) -> tuple[dict[str, str], str]:
    overlay_id = str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, str(root.resolve())))
    return (
        {
            "id": PROJECT_ID,
            "project_path": str(root),
            "parent_project_id": PROJECT_ID,
            "parent_project_path": str(root.parent / "parent"),
        },
        overlay_id,
    )


def test_issue_codes_are_unique_and_stable() -> None:
    assert SYMBOL_TARGET_ISSUE_CODES == {
        "symbol_index_unavailable",
        "symbol_index_stale",
        "target_symbol_required",
        "target_symbol_unresolved",
        "target_symbol_ambiguous",
        "target_wildcard_reason_invalid",
        "target_uuid_forbidden",
        "target_scope_conflict",
        "target_reference_malformed",
        "consumer-coverage",
    }


def test_parse_exact_python_rust_bare_and_wildcard_targets(tmp_path: Path) -> None:
    plan_doc = _parse(
        tmp_path,
        """
        - `src/example.py::Worker.run`
        - `src/lib.rs::Worker::run`
        - `src/new_file.py`
        - `src/generated.py::*` — scope-reason: generated dispatch table
        """,
    )

    targets, issues = parse_symbol_targets(plan_doc)

    assert issues == ()
    assert [target.reference for target in targets] == [
        "src/example.py::Worker.run",
        "src/lib.rs::Worker::run",
        "src/new_file.py",
        "src/generated.py::*",
    ]
    assert targets[1].symbol == "Worker::run"
    assert targets[3].scope_reason == "generated dispatch table"


@pytest.mark.parametrize(
    ("target", "expected_code"),
    [
        ("- `src/example.py::`", MALFORMED_REFERENCE),
        ("- `src/example.py::*`", INVALID_WILDCARD_REASON),
        (
            "- `src/example.py::123e4567-e89b-42d3-a456-426614174000`",
            UUID_REFERENCE,
        ),
        ("- `123e4567-e89b-42d3-a456-426614174000`", UUID_REFERENCE),
        ("- `../src/example.py::run`", MALFORMED_REFERENCE),
        ("- `src/example.py::Worker run`", MALFORMED_REFERENCE),
        (
            "- `src/example.py::run — scope-reason: exact scope`",
            MALFORMED_REFERENCE,
        ),
    ],
)
def test_parse_rejects_malformed_uuid_and_unjustified_wildcard(
    tmp_path: Path,
    target: str,
    expected_code: str,
) -> None:
    _, issues = parse_symbol_targets(_parse(tmp_path, target))

    assert expected_code in {issue.code for issue in issues}


def test_parse_rejects_wildcard_exact_conflict(tmp_path: Path) -> None:
    plan_doc = _parse(
        tmp_path,
        """
        - `src/example.py::Worker.run`
        - `src/example.py::*` — scope-reason: refactor every indexed symbol
        """,
    )

    _, issues = parse_symbol_targets(plan_doc)

    assert SCOPE_CONFLICT in {issue.code for issue in issues}


def test_inline_wildcard_reason_applies_only_to_last_target(tmp_path: Path) -> None:
    plan_doc = _parse(
        tmp_path,
        (
            "`src/example.py::Worker.run`, `src/generated.py::*` "
            "— scope-reason: regenerate the whole registry"
        ),
    )

    targets, issues = parse_symbol_targets(plan_doc)

    assert issues == ()
    assert [target.reference for target in targets] == [
        "src/example.py::Worker.run",
        "src/generated.py::*",
    ]
    assert [target.scope_reason for target in targets] == [None, "regenerate the whole registry"]


def test_scope_reason_code_spans_are_not_parsed_as_targets(tmp_path: Path) -> None:
    reason = "align `_BASE_MODEL_CATALOG` with `/api/providers/models`"
    plan_doc = _parse(
        tmp_path,
        f"`src/providers.py::*` — scope-reason: {reason}",
    )

    targets, issues = parse_symbol_targets(plan_doc)

    assert issues == ()
    assert len(targets) == 1
    assert targets[0].reference == "src/providers.py::*"
    assert targets[0].scope_reason == reason


def test_validate_fresh_exact_multiple_new_and_zero_symbol_files(tmp_path: Path) -> None:
    exact_path = tmp_path / "src" / "example.py"
    zero_path = tmp_path / "src" / "config.toml"
    exact_path.parent.mkdir()
    exact_path.write_text("class Worker:\n    pass\n", encoding="utf-8")
    zero_path.write_text("enabled = true\n", encoding="utf-8")
    index = _Index(
        files={
            "src/example.py": _IndexedFile(_hash(exact_path), 2),
            "src/config.toml": _IndexedFile(_hash(zero_path), 0),
        },
        symbols={
            "src/example.py": [_Symbol("Worker"), _Symbol("Worker.run")],
        },
    )
    plan_doc = _parse(
        tmp_path,
        """
        - `src/example.py::Worker`
        - `src/example.py::Worker.run`
        - `src/config.toml`
        - `src/new_file.py`
        """,
    )

    result = validate_symbol_targets(
        plan_doc,
        project_context=_project_context(tmp_path),
        code_index=index,
        required=True,
    )

    assert result.status == "passed"
    assert result.issues == ()
    assert result.checked_symbols == (
        "src/example.py::Worker",
        "src/example.py::Worker.run",
    )


def test_validate_requires_symbol_scope_for_symbol_bearing_file(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "example.py"
    source_path.parent.mkdir()
    source_path.write_text("def run():\n    pass\n", encoding="utf-8")
    plan_doc = _parse(tmp_path, "- `src/example.py`")
    index = _Index(
        files={"src/example.py": _IndexedFile(_hash(source_path), 1)},
        symbols={"src/example.py": [_Symbol("run")]},
    )

    result = validate_symbol_targets(
        plan_doc,
        project_context=_project_context(tmp_path),
        code_index=index,
        required=True,
    )

    assert result.status == "failed"
    assert MISSING_SYMBOL_SCOPE in _issue_codes(result)


def test_validate_rejects_symbol_scope_for_unindexed_file(tmp_path: Path) -> None:
    result = validate_symbol_targets(
        _parse(tmp_path, "- `src/new_file.py::run`"),
        project_context=_project_context(tmp_path),
        code_index=_Index(),
        required=True,
    )

    assert result.status == "failed"
    assert UNRESOLVED_SYMBOL in _issue_codes(result)


def test_validate_reports_unresolved_and_ambiguous_symbols(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "example.py"
    source_path.parent.mkdir()
    source_path.write_text("def run():\n    pass\n", encoding="utf-8")
    plan_doc = _parse(
        tmp_path,
        """
        - `src/example.py::missing`
        - `src/example.py::duplicate`
        """,
    )
    index = _Index(
        files={"src/example.py": _IndexedFile(_hash(source_path), 3)},
        symbols={
            "src/example.py": [
                _Symbol("run"),
                _Symbol("duplicate"),
                _Symbol("duplicate"),
            ]
        },
    )

    result = validate_symbol_targets(
        plan_doc,
        project_context=_project_context(tmp_path),
        code_index=index,
        required=True,
    )

    assert {UNRESOLVED_SYMBOL, AMBIGUOUS_SYMBOL}.issubset(_issue_codes(result))


def test_validate_stale_index_fails_closed_when_required(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "example.py"
    source_path.parent.mkdir()
    source_path.write_text("def run():\n    pass\n", encoding="utf-8")
    plan_doc = _parse(tmp_path, "- `src/example.py::run`")
    index = _Index(
        files={"src/example.py": _IndexedFile("stale", 1)},
        symbols={"src/example.py": [_Symbol("run")]},
    )

    result = validate_symbol_targets(
        plan_doc,
        project_context=_project_context(tmp_path),
        code_index=index,
        required=True,
    )

    assert result.status == "failed"
    assert INDEX_STALE in _issue_codes(result)


def test_validate_stale_index_skips_when_optional(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "example.py"
    source_path.parent.mkdir()
    source_path.write_text("def run():\n    pass\n", encoding="utf-8")
    plan_doc = _parse(tmp_path, "- `src/example.py::run`")
    index = _Index(files={"src/example.py": _IndexedFile("stale", 1)})

    result = validate_symbol_targets(
        plan_doc,
        project_context=_project_context(tmp_path),
        code_index=index,
        required=False,
    )

    assert result.status == "skipped"
    assert result.errors == []
    assert INDEX_STALE in _issue_codes(result)


@pytest.mark.parametrize(
    ("required", "expected_status", "blocking"),
    [(False, "skipped", False), (True, "failed", True)],
)
def test_validate_missing_index_obeys_policy(
    tmp_path: Path,
    required: bool,
    expected_status: str,
    blocking: bool,
) -> None:
    result = validate_symbol_targets(
        _parse(tmp_path, "- `src/new_file.py`"),
        project_context=_project_context(tmp_path),
        code_index=_Index(available=False),
        required=required,
    )

    assert result.status == expected_status
    assert result.issues[0].code == INDEX_UNAVAILABLE
    assert result.issues[0].blocking is blocking


def test_validate_overlay_resolves_branch_only_and_inherited_files(tmp_path: Path) -> None:
    branch_path = tmp_path / "src" / "branch.py"
    inherited_path = tmp_path / "src" / "inherited.py"
    branch_path.parent.mkdir()
    branch_path.write_text("def branch():\n    pass\n", encoding="utf-8")
    inherited_path.write_text("def inherited():\n    pass\n", encoding="utf-8")
    context, overlay_id = _overlay_context(tmp_path)
    index = _Index(
        project_files={
            overlay_id: {"src/branch.py": _IndexedFile(_hash(branch_path), 1)},
            PROJECT_ID: {"src/inherited.py": _IndexedFile(_hash(inherited_path), 1)},
        },
        project_symbols={
            overlay_id: {"src/branch.py": [_Symbol("branch")]},
            PROJECT_ID: {"src/inherited.py": [_Symbol("inherited")]},
        },
        available_projects={overlay_id, PROJECT_ID},
    )

    result = validate_symbol_targets(
        _parse(
            tmp_path,
            "- `src/branch.py::branch`\n- `src/inherited.py::inherited`",
        ),
        project_context=context,
        code_index=index,
        required=True,
    )

    assert result.status == "passed"
    assert (overlay_id, "src/branch.py") in index.symbol_queries
    assert (PROJECT_ID, "src/inherited.py") in index.symbol_queries


def test_validate_overlay_zero_symbol_state_shadows_parent_symbols(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "config.py"
    source_path.parent.mkdir()
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    context, overlay_id = _overlay_context(tmp_path)
    index = _Index(
        project_files={
            overlay_id: {"src/config.py": _IndexedFile(_hash(source_path), 0)},
            PROJECT_ID: {"src/config.py": _IndexedFile(_hash(source_path), 1)},
        },
        project_symbols={PROJECT_ID: {"src/config.py": [_Symbol("VALUE")]}},
        available_projects={overlay_id, PROJECT_ID},
    )

    result = validate_symbol_targets(
        _parse(tmp_path, "- `src/config.py`"),
        project_context=context,
        code_index=index,
        required=True,
    )

    assert result.status == "passed"
    assert index.symbol_queries == [(overlay_id, "src/config.py")]


def test_validate_stale_overlay_shadows_fresh_parent(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "example.py"
    source_path.parent.mkdir()
    source_path.write_text("def run():\n    pass\n", encoding="utf-8")
    context, overlay_id = _overlay_context(tmp_path)
    index = _Index(
        project_files={
            overlay_id: {"src/example.py": _IndexedFile("stale", 1)},
            PROJECT_ID: {"src/example.py": _IndexedFile(_hash(source_path), 1)},
        },
        project_symbols={PROJECT_ID: {"src/example.py": [_Symbol("run")]}},
        available_projects={overlay_id, PROJECT_ID},
    )

    result = validate_symbol_targets(
        _parse(tmp_path, "- `src/example.py::run`"),
        project_context=context,
        code_index=index,
        required=True,
    )

    assert result.status == "failed"
    assert INDEX_STALE in _issue_codes(result)
    assert (PROJECT_ID, "src/example.py") not in index.file_queries


def test_validate_overlay_tombstone_shadows_parent_file(tmp_path: Path) -> None:
    context, overlay_id = _overlay_context(tmp_path)
    index = _Index(
        project_files={
            overlay_id: {
                "src/deleted.py": _IndexedFile(
                    "__gcode_tombstone__",
                    0,
                    language="__gcode_deleted__",
                )
            },
            PROJECT_ID: {"src/deleted.py": _IndexedFile("parent", 1)},
        },
        project_symbols={PROJECT_ID: {"src/deleted.py": [_Symbol("run")]}},
        available_projects={overlay_id, PROJECT_ID},
    )

    result = validate_symbol_targets(
        _parse(tmp_path, "- `src/deleted.py::run`"),
        project_context=context,
        code_index=index,
        required=True,
    )

    assert result.status == "failed"
    assert INDEX_STALE in _issue_codes(result)
    assert (PROJECT_ID, "src/deleted.py") not in index.file_queries


@pytest.mark.parametrize(
    "project_context",
    [
        {"id": PROJECT_ID, "project_path": "/tmp/worktree", "parent_project_id": PROJECT_ID},
        {
            "id": PROJECT_ID,
            "project_path": "/tmp/worktree",
            "parent_project_id": "different",
            "parent_project_path": "/tmp/parent",
        },
    ],
)
def test_validate_rejects_invalid_isolation_context(
    tmp_path: Path,
    project_context: dict[str, str],
) -> None:
    result = validate_symbol_targets(
        _parse(tmp_path, "- `src/new_file.py`"),
        project_context=project_context,
        code_index=_Index(),
        required=True,
    )

    assert result.status == "failed"
    assert result.issues[0].code == INDEX_UNAVAILABLE


def test_validate_rejects_explicit_project_context_mismatch(tmp_path: Path) -> None:
    result = validate_symbol_targets(
        _parse(tmp_path, "- `src/new_file.py`"),
        project_context=_project_context(tmp_path),
        expected_project_id="different-project",
        code_index=_Index(),
        required=True,
    )

    assert result.status == "failed"
    assert result.issues[0].code == INDEX_UNAVAILABLE


def test_validate_overlay_requires_parent_and_overlay_index_state(tmp_path: Path) -> None:
    context, overlay_id = _overlay_context(tmp_path)
    result = validate_symbol_targets(
        _parse(tmp_path, "- `src/new_file.py`"),
        project_context=context,
        code_index=_Index(available_projects={overlay_id}),
        required=True,
    )

    assert result.status == "failed"
    assert result.issues[0].code == INDEX_UNAVAILABLE
