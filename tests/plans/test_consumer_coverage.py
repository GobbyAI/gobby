"""Tests for code-index-backed plan consumer coverage."""

from __future__ import annotations

import hashlib
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from gobby.code_index.models import CODE_INDEX_UUID_NAMESPACE
from gobby.plans.parser import PlanDocument, parse_plan
from gobby.plans.symbol_targets import SymbolValidationResult, validate_symbol_targets
from gobby.tasks.expansion._validate import validate_plan_file

pytestmark = pytest.mark.unit
PROJECT_ID = "project-1"


def _project_context(root: Path) -> dict[str, str]:
    return {"id": PROJECT_ID, "project_path": str(root)}


@dataclass(frozen=True)
class _IndexedFile:
    content_hash: str
    symbol_count: int


@dataclass(frozen=True)
class _Symbol:
    id: str
    qualified_name: str


@dataclass(frozen=True)
class _ProjectStats:
    root_path: str


class _Index:
    def __init__(
        self,
        root: Path,
        *,
        usages: list[str] | None = None,
        indexed_root: Path | None = None,
        usage_error: bool = False,
    ) -> None:
        self.root = root
        self.usages = usages or []
        self.indexed_root = indexed_root or root
        self.usage_error = usage_error

    def get_project_stats(self, project_id: str) -> _ProjectStats | None:
        assert project_id == PROJECT_ID
        return _ProjectStats(root_path=str(self.indexed_root))

    def get_file(self, project_id: str, file_path: str) -> _IndexedFile | None:
        assert project_id == PROJECT_ID
        path = self.root / file_path
        if not path.exists():
            return None
        return _IndexedFile(
            content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            symbol_count=1 if file_path == "src/provider.py" else 0,
        )

    def get_symbols_for_file(self, project_id: str, file_path: str) -> list[_Symbol]:
        assert project_id == PROJECT_ID
        if file_path == "src/provider.py":
            return [_Symbol(id="provider-run", qualified_name="run")]
        return []

    def get_symbol_usages(self, project_id: str, symbol_id: str) -> list[str]:
        assert project_id == PROJECT_ID
        assert symbol_id == "provider-run"
        if self.usage_error:
            raise psycopg.OperationalError("index offline")
        return self.usages


def _write_source(root: Path, relative_path: str, content: str = "value = 1\n") -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _plan(tmp_path: Path, extra_deliverable: str = "") -> PlanDocument:
    _write_source(tmp_path, "src/provider.py", "def run() -> None:\n    pass\n")
    path = tmp_path / "consumer-plan.md"
    path.write_text(
        textwrap.dedent(
            f"""
            > **Plan ID:** consumer-coverage

            # Consumer Coverage

            ## P1: Work
            `kind: framing`

            ### 1.1 Change provider [category: code]
            `kind: deliverable`

            Target: `src/provider.py::run`

            Implement the provider change.

            **Acceptance:**
            - 1.1.1 - Provider changes. file: `src/provider.py`.

            {extra_deliverable}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return parse_plan(path, parse_mode="draft")


def _validate(
    tmp_path: Path,
    index: _Index,
    *,
    blocking: bool,
    extra_deliverable: str = "",
) -> SymbolValidationResult:
    return validate_symbol_targets(
        _plan(tmp_path, extra_deliverable),
        project_context=_project_context(tmp_path),
        code_index=index,
        required=True,
        consumer_coverage_blocking=blocking,
    )


def test_consumer_coverage_warns_in_base_mode_and_errors_in_expansion(
    tmp_path: Path,
) -> None:
    _write_source(tmp_path, "tests/test_consumer.py")
    index = _Index(tmp_path, usages=["tests/test_consumer.py"])

    base = _validate(tmp_path, index, blocking=False)
    expansion = _validate(tmp_path, index, blocking=True)

    base_issues = [issue for issue in base.issues if issue.code == "consumer-coverage"]
    expansion_issues = [issue for issue in expansion.issues if issue.code == "consumer-coverage"]
    assert len(base_issues) == len(expansion_issues) == 1
    assert base_issues[0].blocking is False
    assert expansion_issues[0].blocking is True
    assert base.status == "passed"
    assert expansion.status == "failed"
    assert "section 1.1" in base_issues[0].message
    assert "src/provider.py::run" in base_issues[0].message
    assert "tests/test_consumer.py" in base_issues[0].message


def test_consumer_coverage_accepts_target_in_any_deliverable(tmp_path: Path) -> None:
    _write_source(tmp_path, "tests/test_consumer.py")
    extra = """
    ### 1.2 Update consumer [category: test] (depends: 1.1)
    `kind: deliverable`

    Target: `tests/test_consumer.py`

    Update the consumer.

    **Acceptance:**
    - 1.2.1 - Consumer changes. test: `tests/test_consumer.py::test_run`.
    """

    result = _validate(
        tmp_path,
        _Index(tmp_path, usages=["tests/test_consumer.py"]),
        blocking=True,
        extra_deliverable=extra,
    )

    assert not any(issue.code == "consumer-coverage" for issue in result.issues)


def test_consumer_coverage_excludes_vendor_node_modules_and_generated_headers(
    tmp_path: Path,
) -> None:
    _write_source(tmp_path, "generated/client.py", "# Generated file; do not edit.\nvalue = 1\n")
    index = _Index(
        tmp_path,
        usages=[
            "vendor/library.py",
            "web/node_modules/package/index.js",
            "generated/client.py",
        ],
    )

    result = _validate(tmp_path, index, blocking=True)

    assert not any(issue.code == "consumer-coverage" for issue in result.issues)


def test_consumer_coverage_skips_worktree_not_covered_by_index(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /repo/.git/worktrees/test\n", encoding="utf-8")
    index = _Index(
        tmp_path,
        usages=["tests/test_consumer.py"],
        indexed_root=tmp_path / "main-checkout",
    )

    result = _validate(tmp_path, index, blocking=True)

    issues = [issue for issue in result.issues if issue.code == "consumer-coverage"]
    assert len(issues) == 1
    assert issues[0].blocking is False
    assert issues[0].message.startswith("consumer-coverage skipped:")
    assert "#20664" in issues[0].message


def test_consumer_coverage_usage_read_failure_emits_one_skip_warning(tmp_path: Path) -> None:
    result = _validate(tmp_path, _Index(tmp_path, usage_error=True), blocking=True)

    issues = [issue for issue in result.issues if issue.code == "consumer-coverage"]
    assert len(issues) == 1
    assert issues[0].blocking is False
    assert issues[0].message == "consumer-coverage skipped: code index usages are unavailable"


def test_consumer_coverage_missing_index_emits_one_nonblocking_skip(tmp_path: Path) -> None:
    class _UnavailableIndex(_Index):
        def get_project_stats(self, project_id: str) -> None:
            assert project_id == PROJECT_ID
            return None

    result = validate_symbol_targets(
        _plan(tmp_path),
        project_context=_project_context(tmp_path),
        code_index=_UnavailableIndex(tmp_path),
        required=False,
        consumer_coverage_blocking=True,
    )

    issues = [issue for issue in result.issues if issue.code == "consumer-coverage"]
    assert len(issues) == 1
    assert issues[0].blocking is False
    assert issues[0].message.startswith("consumer-coverage skipped:")
    assert result.errors == []


@pytest.mark.parametrize(
    ("blocking", "expected_valid", "result_key"),
    [(False, True, "warnings"), (True, False, "errors")],
)
def test_plan_validation_surfaces_consumer_coverage_by_mode(
    tmp_path: Path,
    blocking: bool,
    expected_valid: bool,
    result_key: str,
) -> None:
    _write_source(tmp_path, "tests/test_consumer.py")
    plan_doc = _plan(tmp_path)

    result = validate_plan_file(
        None,
        plan_doc.source_path,
        project_context=_project_context(tmp_path),
        code_index=_Index(tmp_path, usages=["tests/test_consumer.py"]),
        require_symbol_validation=True,
        consumer_coverage_blocking=blocking,
    )

    assert result["valid"] is expected_valid
    assert any("consumer-coverage" in message for message in result[result_key])


def test_plan_validation_surfaces_semantic_lint_warnings_without_project_root(
    tmp_path: Path,
) -> None:
    plan_doc = _plan(tmp_path)

    result = validate_plan_file(None, plan_doc.source_path)

    assert result["valid"] is True
    assert "production-size-growth skipped: no project root" in result["warnings"]


def test_consumer_coverage_unions_overlay_and_parent_usages(tmp_path: Path) -> None:
    overlay_project_id = str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, str(tmp_path.resolve())))
    usages_by_project = {
        overlay_project_id: ["tests/test_overlay_consumer.py"],
        PROJECT_ID: ["tests/test_parent_consumer.py"],
    }

    class _OverlayIndex(_Index):
        def get_project_stats(self, project_id: str) -> _ProjectStats | None:
            assert project_id in usages_by_project
            return _ProjectStats(root_path=str(self.indexed_root))

        def get_file(self, project_id: str, file_path: str) -> _IndexedFile | None:
            assert project_id in usages_by_project
            return super().get_file(PROJECT_ID, file_path)

        def get_symbols_for_file(self, project_id: str, file_path: str) -> list[_Symbol]:
            assert project_id in usages_by_project
            return super().get_symbols_for_file(PROJECT_ID, file_path)

        def get_symbol_usages(self, project_id: str, symbol_id: str) -> list[str]:
            assert symbol_id == "provider-run"
            return usages_by_project[project_id]

    _write_source(tmp_path, "tests/test_overlay_consumer.py")
    _write_source(tmp_path, "tests/test_parent_consumer.py")
    result = validate_symbol_targets(
        _plan(tmp_path),
        project_context={
            **_project_context(tmp_path),
            "parent_project_id": PROJECT_ID,
            "parent_project_path": str(tmp_path / "parent-checkout"),
        },
        code_index=_OverlayIndex(tmp_path),
        required=True,
        consumer_coverage_blocking=True,
    )

    issues = [issue for issue in result.issues if issue.code == "consumer-coverage"]
    assert len(issues) == 1
    assert "tests/test_overlay_consumer.py" in issues[0].message
    assert "tests/test_parent_consumer.py" in issues[0].message


_UNCHANGED = (
    "Consumers unchanged:\n- `src/caller.py` — no-edit-reason: Existing call remains valid."
)


def _plan_with_unchanged(tmp_path: Path, inventory: str = _UNCHANGED) -> PlanDocument:
    plan = _plan(tmp_path)
    text = plan.source_path.read_text().replace("Implement the provider change.", inventory)
    plan.source_path.write_text(text)
    return parse_plan(plan.source_path, parse_mode="draft")


def test_unchanged_consumer_covers_only_owning_symbol_without_edit_scope(tmp_path: Path) -> None:
    from gobby.plans.semantic_lint import collect_strict_target_inventory, lint_plan_document
    from gobby.tasks.expansion._common import _contract_section_body

    _write_source(tmp_path, "src/caller.py", "# big unchanged consumer\n" * 900)
    _write_source(tmp_path, "src/omitted.py")
    plan = _plan_with_unchanged(tmp_path)
    result = validate_symbol_targets(
        plan,
        project_context=_project_context(tmp_path),
        code_index=_Index(tmp_path, usages=["src/caller.py", "src/omitted.py"]),
        required=True,
        consumer_coverage_blocking=True,
    )
    assert result.status == "failed"
    assert "src/omitted.py" in " ".join(result.errors)
    assert "src/caller.py" not in " ".join(result.errors)
    section = next(s for s in plan.sections if s.section_id == "1.1")
    assert collect_strict_target_inventory(plan, section) == frozenset({"src/provider.py"})
    assert lint_plan_document(plan, project_root=tmp_path).valid
    assert _UNCHANGED in _contract_section_body(plan, section)


def test_unchanged_consumer_in_another_section_does_not_cover_symbol(tmp_path: Path) -> None:
    _write_source(tmp_path, "src/caller.py")
    plan = _plan(tmp_path)
    plan.source_path.write_text(
        plan.source_path.read_text()
        + """### 1.2 Verify caller [category: docs]
`kind: deliverable`

Target: `docs/new.md`

Consumers unchanged:
- `src/caller.py` — no-edit-reason: Existing call remains valid.

**Acceptance:**
- 1.2.1 - Document verification. file: `docs/new.md`.
""",
    )
    plan = parse_plan(plan.source_path, parse_mode="draft")
    result = validate_symbol_targets(
        plan,
        project_context=_project_context(tmp_path),
        code_index=_Index(tmp_path, usages=["src/caller.py"]),
        required=True,
        consumer_coverage_blocking=True,
    )
    assert result.status == "failed"
    assert any(i.code == "consumer-coverage" and i.section_id == "1.1" for i in result.issues)


@pytest.mark.parametrize(
    "entry",
    [
        "- `../caller.py` — no-edit-reason: Existing call.",
        "- `/tmp/caller.py` — no-edit-reason: Existing call.",
        "- `src/./caller.py` — no-edit-reason: Existing call.",
        "- `src/caller.py::run` — no-edit-reason: Existing call.",
        "- `src/*.py` — no-edit-reason: Existing call.",
        "- `src/missing.py` — no-edit-reason: Existing call.",
        "- `src/caller.py` — no-edit-reason:",
        "- `src/caller.py`",
        "- `src/provider.py` — no-edit-reason: Existing call.",
        "\n- `src/caller.py` — no-edit-reason: Block separated by blank line.",
    ],
)
def test_unchanged_consumer_rejects_invalid_inventory(tmp_path: Path, entry: str) -> None:
    from gobby.plans.semantic_lint import lint_plan_document

    _write_source(tmp_path, "src/caller.py")
    plan = _plan_with_unchanged(tmp_path, "Consumers unchanged:\n" + entry)
    result = lint_plan_document(plan, project_root=tmp_path)
    assert not result.valid
    assert any(i.code == "consumers-unchanged" for i in result.issues)


def test_unchanged_consumer_rejects_conflict_in_other_section_and_missing_root(
    tmp_path: Path,
) -> None:
    from gobby.plans.semantic_lint import lint_plan_document

    _write_source(tmp_path, "src/caller.py")
    plan = _plan_with_unchanged(tmp_path)
    no_root = lint_plan_document(plan)
    assert any("without a project root" in error for error in no_root.errors)
    with plan.source_path.open("a") as output:
        output.write("""
### 1.2 Edit caller [category: code]
`kind: deliverable`
Target: `src/caller.py`

**Acceptance:**
- 1.2.1 - Caller changes. file: `src/caller.py`.
""")
    result = lint_plan_document(
        parse_plan(plan.source_path, parse_mode="draft"), project_root=tmp_path
    )
    assert any("conflicts with an edit Target" in error for error in result.errors)


@pytest.mark.parametrize("inline", [False, True])
def test_unchanged_inventory_never_enters_expanded_task_scope(tmp_path: Path, inline: bool) -> None:
    from gobby.mcp_proxy.tools.tasks._task_scope import collect_declared_task_targets
    from gobby.plans.semantic_lint import collect_strict_target_inventory
    from gobby.tasks.expansion._common import _contract_section_body

    _write_source(tmp_path, "src/caller.py")
    inventory = _UNCHANGED.replace(":\n", ": ") if inline else _UNCHANGED
    plan = _plan_with_unchanged(tmp_path, inventory)
    text = plan.source_path.read_text().replace("::run`\n\nConsumers", "::run`\nConsumers")
    plan.source_path.write_text(text)
    plan = parse_plan(plan.source_path, parse_mode="draft")
    section = next(s for s in plan.sections if s.section_id == "1.1")
    body = _contract_section_body(plan, section)
    assert inventory in body
    assert collect_declared_task_targets(body) == {"src/provider.py"}
    assert collect_strict_target_inventory(plan, section) == frozenset({"src/provider.py"})


def test_unchanged_consumer_rejects_symlink_outside_repository(tmp_path: Path) -> None:
    from gobby.plans.semantic_lint import lint_plan_document

    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "caller.py"
    outside.write_text("pass\n")
    plan = _plan_with_unchanged(root)
    (root / "src/caller.py").symlink_to(outside)
    result = lint_plan_document(plan, project_root=root)
    assert any("inside the repository" in error for error in result.errors)
