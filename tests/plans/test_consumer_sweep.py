"""Tests for code-index-backed plan consumer sweep."""

from __future__ import annotations

import importlib
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.agents.code_index import (
    IndexInventoryError,
    repository_source_digest,
    settle_indexed_value,
)
from gobby.mcp_proxy.tools.plans import create_plan_registry
from gobby.plans import consumer_sweep as consumer_sweep_module
from gobby.plans.consumer_sweep import (
    ConsumerInventoryError,
    ConsumerSweepResult,
    _destructive_target_paths,
    derive_candidate_site_inventory,
    run_consumer_sweep,
)
from gobby.plans.parser import parse_plan
from gobby.plans.review_evidence_io import (
    build_inter_round_diff,
    with_consumer_inventory_context,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.tasks.expansion import _plan_gate as plan_gate

pytestmark = pytest.mark.unit

cli_plans = importlib.import_module("gobby.cli.plans")
_DEFERRAL_SYMBOL_REF = "gobby.plans.deferral.validate_deferral"
_DEFERRAL_TARGET = "src/gobby/plans/deferral.py"


@dataclass(frozen=True)
class _Symbol:
    id: str
    name: str
    qualified_name: str
    file_path: str


class _Storage:
    def __init__(self, *, indexed: bool = True) -> None:
        self.indexed = indexed
        self.symbols: dict[str, tuple[_Symbol, ...]] = {
            "app.service.do_work": (
                _Symbol(
                    id="sym-do-work",
                    name="do_work",
                    qualified_name="app.service.do_work",
                    file_path="src/service.py",
                ),
            )
        }
        self.callers: dict[str, tuple[str, ...]] = {"sym-do-work": ("src/api.py",)}
        self.file_consumers: dict[str, tuple[str, ...]] = {}
        self.import_consumers: dict[str, tuple[str, ...]] = {}
        self.depth_two_callers: dict[str, tuple[str, ...]] = {
            "sym-do-work": ("src/cli.py",),
        }
        self.search_queries: list[str] = []
        self.caller_queries: list[tuple[str, ...]] = []

    def get_project_stats(self, project_id: str) -> object | None:
        return object() if self.indexed and project_id == "project-1" else None

    def search_symbols_by_name(
        self,
        query: str,
        project_id: str,
        kind: str | None = None,
        file_path: str | None = None,
        limit: int = 50,
    ) -> tuple[_Symbol, ...]:
        del kind, file_path, limit
        self.search_queries.append(query)
        if project_id != "project-1":
            return ()
        return self.symbols.get(query, ())

    def find_direct_callers(
        self,
        project_id: str,
        symbol_ids: tuple[str, ...],
        callee_names: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        del callee_names
        self.caller_queries.append(symbol_ids)
        if project_id != "project-1":
            return []
        return [
            {"file_path": caller}
            for symbol_id in symbol_ids
            for caller in self.callers.get(symbol_id, ())
        ]

    def get_symbols_for_file(self, project_id: str, file_path: str) -> tuple[_Symbol, ...]:
        if project_id != "project-1":
            return ()
        return tuple(
            symbol
            for symbols in self.symbols.values()
            for symbol in symbols
            if symbol.file_path == file_path
        )

    def find_direct_file_consumers(
        self,
        project_id: str,
        file_path: str,
        module_candidates: tuple[str, ...],
        symbol_ids: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        del module_candidates, symbol_ids
        if project_id != "project-1":
            return []
        return [{"file_path": caller} for caller in self.file_consumers.get(file_path, ())]

    def find_files_importing_modules(
        self,
        project_id: str,
        module_candidates: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if project_id != "project-1":
            return []
        return [
            {"file_path": caller}
            for module in module_candidates
            for caller in self.import_consumers.get(module, ())
        ]


@dataclass(frozen=True)
class _CodeIndex:
    storage: object
    graph: object = object()


def _leaf_only_storage() -> _Storage:
    storage = _Storage()
    storage.symbols = {
        "validate_deferral": (
            _Symbol(
                id="sym-validate-deferral",
                name="validate_deferral",
                qualified_name="validate_deferral",
                file_path=_DEFERRAL_TARGET,
            ),
        )
    }
    storage.callers = {"sym-validate-deferral": ("src/gobby/plans/consumer.py",)}
    return storage


def _write_plan(
    tmp_path: Path,
    targets: str,
    *,
    symbol_ref: str = "app.service.do_work",
    file_path: str = "src/service.py",
) -> Path:
    path = tmp_path / "plan.md"
    header = textwrap.dedent(
        """
        > **Plan ID:** consumer-sweep

        # Consumer Sweep

        ## P1: Work
        `kind: framing`

        ### 1.1 Rename Service [category: code]
        `kind: deliverable`

        Targets:
        """
    ).lstrip()
    body = textwrap.dedent(
        f"""

        Rename symbol: `{symbol_ref}` and update the implementation.

        **Acceptance:**
        - 1.1.1 - Service symbol is renamed. symbol: `{symbol_ref}`.
        - 1.1.2 - Service file changes. file: `{file_path}`.
        """
    )
    path.write_text(
        header + textwrap.indent(targets, "        ") + body,
        encoding="utf-8",
    )
    return path


def _write_two_section_plan(
    tmp_path: Path,
    *,
    first_targets: tuple[str, ...],
    first_symbol: str,
    second_targets: tuple[str, ...],
    second_symbol: str,
) -> Path:
    path = tmp_path / "sectioned-plan.md"
    header = textwrap.dedent(
        """
        > **Plan ID:** sectioned-consumer-sweep

        # Sectioned Consumer Sweep

        ## P1: Work
        `kind: framing`

        ### 1.1 First Change [category: code]
        `kind: deliverable`

        Targets:
        """
    ).lstrip()
    middle = textwrap.dedent(
        f"""

        Update symbol: `{first_symbol}`.

        **Acceptance:**
        - 1.1.1 - First symbol changes. symbol: `{first_symbol}`.
        - 1.1.2 - First file changes. file: `{first_targets[0]}`.

        ### 1.2 Second Change [category: code]
        `kind: deliverable`

        Targets:
        """
    )
    footer = textwrap.dedent(
        f"""

        Update symbol: `{second_symbol}`.

        **Acceptance:**
        - 1.2.1 - Second symbol changes. symbol: `{second_symbol}`.
        - 1.2.2 - Second file changes. file: `{second_targets[0]}`.
        """
    )
    path.write_text(
        header
        + textwrap.indent(
            "\n".join(f"- `{target}`" for target in first_targets),
            "        ",
        )
        + middle
        + textwrap.indent(
            "\n".join(f"- `{target}`" for target in second_targets),
            "        ",
        )
        + footer,
        encoding="utf-8",
    )
    return path


def _run_leaf_symbol_sweep(
    tmp_path: Path,
    storage: _Storage,
) -> ConsumerSweepResult:
    plan = parse_plan(
        _write_plan(
            tmp_path,
            f"- `{_DEFERRAL_TARGET}`",
            symbol_ref=_DEFERRAL_SYMBOL_REF,
            file_path=_DEFERRAL_TARGET,
        ),
        parse_mode="draft",
    )
    return run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )


def _write_file_plan(
    tmp_path: Path,
    body: str,
    *,
    target_line: str = "Target: `src/service.py`",
) -> Path:
    path = tmp_path / "file-plan.md"
    path.write_text(
        f"""> **Plan ID:** consumer-file-sweep

# Consumer File Sweep

## P1: Work
`kind: framing`

### 1.1 Service File [category: code]
`kind: deliverable`

{target_line}

{body}

**Acceptance:**
- 1.1.1 - Service file changes. file: `src/service.py`.
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "marker",
    [
        "deletions only",
        "Delete File",
        "remove file",
        "DROP FILE",
        "Renamed File",
        "MOVED\tFILE",
        "deleted entirely",
        "Removed Entirely",
    ],
)
def test_destructive_file_marker_matrix(marker: str) -> None:
    assert _destructive_target_paths(f"`src/service.py` ({marker})") == {"src/service.py"}


@pytest.mark.parametrize("near_miss", ["UNRENAMED FILE", "MOVED FILES"])
def test_destructive_file_marker_respects_word_boundaries(near_miss: str) -> None:
    assert _destructive_target_paths(f"`src/service.py` ({near_miss})") == set()


@pytest.mark.parametrize("marker", ["RENAMED FILE", "MOVED FILE"])
def test_renamed_and_moved_markers_trigger_file_level_sweep(
    tmp_path: Path,
    marker: str,
) -> None:
    storage = _Storage()
    storage.file_consumers = {"src/service.py": ("src/api.py",)}
    plan = parse_plan(
        _write_file_plan(
            tmp_path,
            "Update the service file.",
            target_line=f"Target: `src/service.py` ({marker})",
        ),
        parse_mode="draft",
    )

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )

    assert result.valid is False
    assert result.issues[0].missing_consumers == ("src/api.py",)


def test_destructive_symbol_change_with_unlisted_direct_caller_fails(tmp_path: Path) -> None:
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(_Storage()),
    )

    assert result.valid is False
    assert any("src/api.py" in error for error in result.errors)


def test_module_qualified_symbol_requeries_leaf_only_index(tmp_path: Path) -> None:
    storage = _leaf_only_storage()
    result = _run_leaf_symbol_sweep(tmp_path, storage)

    assert storage.search_queries == [_DEFERRAL_SYMBOL_REF, "validate_deferral"]
    assert storage.caller_queries == [("sym-validate-deferral",)]
    assert result.valid is False
    assert any("src/gobby/plans/consumer.py" in error for error in result.errors)


@pytest.mark.parametrize(
    "symbols",
    [
        (
            _Symbol(
                id="sym-validate-deferral",
                name="validate_deferral",
                qualified_name="validate_deferral",
                file_path="src/gobby/plans/other.py",
            ),
        ),
        (
            _Symbol(
                id="sym-validate-deferral",
                name="validate_deferral",
                qualified_name="validate_deferral",
                file_path=_DEFERRAL_TARGET,
            ),
            _Symbol(
                id="sym-other-validate-deferral",
                name="validate_deferral",
                qualified_name="validate_deferral",
                file_path="src/gobby/plans/other.py",
            ),
        ),
    ],
    ids=["defined-outside-targets", "ambiguous-exact-names"],
)
def test_leaf_fallback_requires_unique_symbol_defined_in_target(
    tmp_path: Path,
    symbols: tuple[_Symbol, ...],
) -> None:
    storage = _leaf_only_storage()
    storage.symbols["validate_deferral"] = symbols
    result = _run_leaf_symbol_sweep(tmp_path, storage)

    assert storage.search_queries == [_DEFERRAL_SYMBOL_REF, "validate_deferral"]
    assert storage.caller_queries == []
    assert result.valid is True


def test_listed_direct_caller_passes(tmp_path: Path) -> None:
    plan = parse_plan(
        _write_plan(tmp_path, "- `src/service.py`\n- `src/api.py`"),
        parse_mode="draft",
    )

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(_Storage()),
    )

    assert result.valid is True
    assert result.skipped is False


def test_test_consumers_can_be_excluded(tmp_path: Path) -> None:
    storage = _Storage()
    storage.callers = {"sym-do-work": ("src/api.py", "tests/test_service.py")}
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
        include_tests=False,
    )

    assert result.valid is False
    assert len(result.issues) == 1
    assert result.issues[0].missing_consumers == ("src/api.py",)
    assert all("tests/test_service.py" not in error for error in result.errors)


def test_symbol_defined_outside_targets_does_not_fail(tmp_path: Path) -> None:
    storage = _Storage()
    storage.symbols = {
        "app.service.do_work": (
            _Symbol(
                id="sym-do-work",
                name="do_work",
                qualified_name="app.service.do_work",
                file_path="src/other.py",
            ),
        )
    }
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )

    assert result.valid is True


def test_non_destructive_file_change_does_not_enumerate_importers(tmp_path: Path) -> None:
    storage = _Storage()
    storage.file_consumers = {"src/service.py": ("src/api.py",)}
    plan = parse_plan(
        _write_file_plan(tmp_path, "Update `src/service.py` implementation details."),
        parse_mode="draft",
    )

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )

    assert result.valid is True


def test_file_deletion_prose_without_target_annotation_does_not_enumerate_importers(
    tmp_path: Path,
) -> None:
    storage = _Storage()
    storage.file_consumers = {"src/service.py": ("src/api.py",)}
    plan = parse_plan(_write_file_plan(tmp_path, "Delete `src/service.py`."), parse_mode="draft")

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )

    assert result.valid is True


def test_destructive_file_intent_excludes_test_consumers(tmp_path: Path) -> None:
    storage = _Storage()
    storage.file_consumers = {"src/service.py": ("src/api.py", "tests/test_service.py")}
    plan = parse_plan(
        _write_file_plan(
            tmp_path,
            "Remove obsolete service code.",
            target_line=(
                "Target: `src/service.py` (DELETIONS ONLY - remove obsolete file-level surface)"
            ),
        ),
        parse_mode="draft",
    )

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
        include_tests=False,
    )

    assert result.valid is False
    assert len(result.issues) == 1
    assert result.issues[0].missing_consumers == ("src/api.py",)
    assert all("tests/test_service.py" not in error for error in result.errors)


def test_pathless_target_bullet_does_not_hide_later_file_deletion(tmp_path: Path) -> None:
    storage = _Storage()
    storage.file_consumers = {"src/b.py": ("src/api.py",)}
    plan = parse_plan(
        _write_file_plan(
            tmp_path,
            "Remove the obsolete service file.",
            target_line=(
                "Targets:\n"
                "- Coordinate the rollout with downstream owners.\n"
                "- `src/b.py` (DELETE FILE)"
            ),
        ),
        parse_mode="draft",
    )

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )

    assert result.valid is False
    assert len(result.issues) == 1
    assert result.issues[0].missing_consumers == ("src/api.py",)


def test_missing_index_skips_without_failing(tmp_path: Path) -> None:
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    result = run_consumer_sweep(plan, project_id=None, code_index=None)

    assert result.valid is True
    assert result.skipped is True


def test_inventory_unavailable_raises_typed_error(tmp_path: Path) -> None:
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    with pytest.raises(ConsumerInventoryError, match="code index") as raised:
        run_consumer_sweep(
            plan,
            project_id="project-1",
            code_index=_CodeIndex(_Storage(indexed=False)),
        )

    assert raised.value.code == "inventory_unavailable"


def test_depth_two_only_consumer_does_not_fail_v1(tmp_path: Path) -> None:
    storage = _Storage()
    storage.callers = {"sym-do-work": ()}
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )

    assert result.valid is True


class _StorageWithoutStats:
    pass


def test_storage_without_supported_stats_api_is_unavailable(tmp_path: Path) -> None:
    plan = parse_plan(_write_plan(tmp_path, "- `src/service.py`"), parse_mode="draft")

    with pytest.raises(ConsumerInventoryError) as raised:
        run_consumer_sweep(
            plan,
            project_id="project-1",
            code_index=_CodeIndex(_StorageWithoutStats()),
        )

    assert raised.value.code == "inventory_unavailable"


def test_repeated_target_path_does_not_bleed_destructive_annotation(
    tmp_path: Path,
) -> None:
    storage = _Storage()
    storage.file_consumers = {"src/helper.py": ("src/helper_consumer.py",)}
    plan = parse_plan(
        _write_file_plan(
            tmp_path,
            "Remove obsolete service code.",
            target_line=(
                "Targets: `src/service.py` `src/helper.py` `src/service.py` (DELETIONS ONLY)"
            ),
        ),
        parse_mode="draft",
    )

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )

    assert result.valid is True


def test_import_edge_file_consumers(tmp_path: Path) -> None:
    storage = _Storage()
    storage.import_consumers = {"app.service": ("src/importer.py",)}
    plan = parse_plan(
        _write_file_plan(
            tmp_path,
            "Rename file `src/app/service.py`.",
            target_line="Target: `src/app/service.py` (RENAMED FILE)",
        ),
        parse_mode="draft",
    )

    result = run_consumer_sweep(
        plan,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )

    assert result.valid is False
    assert result.issues[0].missing_consumers == ("src/importer.py",)


def test_inter_round_site_inventory(tmp_path: Path) -> None:
    plan_path = _write_plan(
        tmp_path,
        "- `src/service.py`",
        symbol_ref="app.service.do_work",
    )
    prior_snapshot = plan_path.read_bytes()
    _write_plan(
        tmp_path,
        "- `src/service.py`\n- `contracts/review-result.json`",
        symbol_ref="app.service.new_work",
    )
    current_snapshot = plan_path.read_bytes()
    current = parse_plan(plan_path, parse_mode="draft")
    storage = _Storage()
    storage.symbols["app.service.new_work"] = (
        _Symbol(
            id="sym-new-work",
            name="new_work",
            qualified_name="app.service.new_work",
            file_path="src/service.py",
        ),
    )
    storage.callers["sym-new-work"] = ("src/api.py",)

    diff = build_inter_round_diff(prior_snapshot, current_snapshot)
    sweep = run_consumer_sweep(
        current,
        diff=diff,
        project_id="project-1",
        code_index=_CodeIndex(storage),
    )
    assert sweep.inventory is not None
    inventory = sweep.inventory
    context = with_consumer_inventory_context(
        {"prior_evidence_id": "prior-1"},
        inventory=inventory.to_dict(),
    )

    assert inventory.changed_acceptance_item_ids == ("1.1.1",)
    assert inventory.changed_symbols == (
        "app.service.do_work",
        "app.service.new_work",
    )
    assert inventory.changed_contracts == ("contracts/review-result.json",)
    assert any(site.path == "src/api.py" for site in inventory.sites)
    assert all(site.section_ids == ("1.1",) for site in inventory.sites)
    assert context["consumer_site_inventory"] == inventory.to_dict()
    assert "index_token" not in context


def test_inter_round_inventory_preserves_section_attribution(tmp_path: Path) -> None:
    plan_path = _write_two_section_plan(
        tmp_path,
        first_targets=("src/old_first.py",),
        first_symbol="app.first.old",
        second_targets=("src/old_second.py",),
        second_symbol="app.second.old",
    )
    prior_snapshot = plan_path.read_bytes()
    _write_two_section_plan(
        tmp_path,
        first_targets=("src/new_first.py", "src/shared.py"),
        first_symbol="app.first.new",
        second_targets=("src/new_second.py", "src/shared.py"),
        second_symbol="app.second.new",
    )
    current_snapshot = plan_path.read_bytes()
    storage = _Storage()
    storage.file_consumers = {
        "src/new_first.py": ("src/first_file_consumer.py",),
        "src/new_second.py": ("src/second_file_consumer.py",),
        "src/shared.py": ("src/shared_consumer.py",),
    }
    storage.symbols.update(
        {
            "app.first.new": (
                _Symbol(
                    id="sym-first-new",
                    name="new",
                    qualified_name="app.first.new",
                    file_path="src/new_first.py",
                ),
            ),
            "app.second.new": (
                _Symbol(
                    id="sym-second-new",
                    name="new",
                    qualified_name="app.second.new",
                    file_path="src/new_second.py",
                ),
            ),
        }
    )
    storage.callers.update(
        {
            "sym-first-new": ("src/first_symbol_consumer.py",),
            "sym-second-new": ("src/second_symbol_consumer.py",),
        }
    )

    diff = build_inter_round_diff(prior_snapshot, current_snapshot)
    inventory = derive_candidate_site_inventory(
        diff=diff,
        project_id="project-1",
        storage=storage,
    )

    assert diff.targets_by_section == {
        "1.1": ("src/new_first.py", "src/old_first.py", "src/shared.py"),
        "1.2": ("src/new_second.py", "src/old_second.py", "src/shared.py"),
    }
    assert diff.symbols_by_section == {
        "1.1": ("app.first.new", "app.first.old"),
        "1.2": ("app.second.new", "app.second.old"),
    }
    sites = {(site.path, site.source_kind, site.source_ref): site for site in inventory.sites}
    assert sites[
        ("src/first_file_consumer.py", "file_consumer", "src/new_first.py")
    ].section_ids == ("1.1",)
    assert sites[("src/first_symbol_consumer.py", "symbol_call", "app.first.new")].section_ids == (
        "1.1",
    )
    assert sites[
        ("src/second_file_consumer.py", "file_consumer", "src/new_second.py")
    ].section_ids == ("1.2",)
    assert sites[
        ("src/second_symbol_consumer.py", "symbol_call", "app.second.new")
    ].section_ids == ("1.2",)
    shared_sites = [
        site
        for site in inventory.sites
        if site.path == "src/shared_consumer.py" and site.source_ref == "src/shared.py"
    ]
    assert len(shared_sites) == 1
    assert shared_sites[0].section_ids == ("1.1", "1.2")
    assert shared_sites[0].to_dict()["section_ids"] == ["1.1", "1.2"]


def test_index_token_brackets_index_operation(tmp_path: Path) -> None:
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("version = 1\n", encoding="utf-8")
    index_calls = 0
    derive_calls = 0

    def index_operation() -> None:
        nonlocal index_calls
        index_calls += 1
        if index_calls == 1:
            source.write_text("version = 2\n", encoding="utf-8")

    def derive_inventory() -> str:
        nonlocal derive_calls
        derive_calls += 1
        return "inventory"

    value = settle_indexed_value(
        tmp_path,
        index_operation=index_operation,
        read_last_indexed_at=lambda: "2026-07-27T00:00:00+00:00",
        derive=derive_inventory,
        source_files=("src/service.py",),
        backoff_seconds=0,
    )

    assert value == "inventory"
    assert index_calls == 2
    assert derive_calls == 1


def test_settle_ignores_repository_change_after_derivation(tmp_path: Path) -> None:
    """Churn after the bracketed index run is invisible.

    Repository state moves constantly during planning. The bracket only has to
    prove the derivation saw a coherent index; nothing pins that state
    afterwards, so a later edit cannot invalidate an in-flight round.
    """
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("version = 1\n", encoding="utf-8")

    value = settle_indexed_value(
        tmp_path,
        index_operation=lambda: None,
        read_last_indexed_at=lambda: "2026-07-27T00:00:00+00:00",
        derive=lambda: "inventory",
        source_files=("src/service.py",),
        backoff_seconds=0,
    )
    assert value == "inventory"

    source.write_text("version = 2\n", encoding="utf-8")
    digest = repository_source_digest(tmp_path, source_files=("src/service.py",))
    assert digest.digest  # the repository moved, and nothing recorded it


async def test_index_verifier_wrapper_is_not_registered(temp_db: HubDatabase) -> None:
    project_id = LocalProjectManager(temp_db).create(name="index-token-verifier").id
    registry = create_plan_registry(temp_db, default_project_id=project_id)

    assert "verify_plan_review_index_token" not in {tool["name"] for tool in registry.list_tools()}


def test_unsupported_language_marked_not_omitted(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path, "- `src/service.py`")
    prior_snapshot = plan_path.read_bytes()
    _write_plan(tmp_path, "- `src/service.py`\n- `web/review.ts`")
    current_snapshot = plan_path.read_bytes()

    inventory = derive_candidate_site_inventory(
        diff=build_inter_round_diff(prior_snapshot, current_snapshot),
        project_id="project-1",
        storage=_Storage(),
    )

    assert inventory.unsupported_targets == ("web/review.ts",)
    assert any(
        site.path == "web/review.ts"
        and site.status == "language_unsupported"
        and site.language == "typescript"
        for site in inventory.sites
    )


def test_index_settle_retry_is_bounded(tmp_path: Path) -> None:
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("version = 0\n", encoding="utf-8")
    index_calls = 0

    def mutate_during_index() -> None:
        nonlocal index_calls
        index_calls += 1
        source.write_text(f"version = {index_calls}\n", encoding="utf-8")

    with pytest.raises(IndexInventoryError) as raised:
        settle_indexed_value(
            tmp_path,
            index_operation=mutate_during_index,
            read_last_indexed_at=lambda: "2026-07-27T00:00:00+00:00",
            derive=lambda: "unreachable",
            source_files=("src/service.py",),
            max_attempts=3,
            timeout_seconds=60,
            backoff_seconds=0,
        )

    assert raised.value.code == "index_unstable"
    assert index_calls == 3

    deadline_calls = 0
    clock = iter((0.0, 0.0, 61.0))

    def indexed_once() -> None:
        nonlocal deadline_calls
        deadline_calls += 1

    with pytest.raises(IndexInventoryError) as deadline:
        settle_indexed_value(
            tmp_path,
            index_operation=indexed_once,
            read_last_indexed_at=lambda: "2026-07-27T00:00:00+00:00",
            derive=lambda: "unreachable",
            source_files=("src/service.py",),
            max_attempts=3,
            timeout_seconds=60,
            backoff_seconds=0,
            monotonic=lambda: next(clock),
        )

    assert deadline.value.code == "index_unstable"
    assert deadline_calls == 1


def test_typed_sweep_error_handled_by_both_callers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = _write_plan(tmp_path, "- `src/service.py`")

    def unavailable(*args: object, **kwargs: object) -> ConsumerSweepResult:
        del args, kwargs
        raise ConsumerInventoryError(
            "inventory_unavailable",
            "code index is unavailable",
        )

    monkeypatch.setattr(cli_plans, "run_consumer_sweep", unavailable)
    monkeypatch.setattr(consumer_sweep_module, "run_consumer_sweep", unavailable)
    monkeypatch.setattr(cli_plans, "resolve_project_ref", lambda _project: None)

    cli_result = cli_plans._validate_plan_for_cli(
        plan_path,
        None,
        include_tests=True,
    )
    manager = SimpleNamespace(
        get_artifacts=lambda _task_id: SimpleNamespace(plan_file_path=str(plan_path)),
        get_task=lambda _task_id: {"project_id": "project-1"},
    )
    spawn_result = plan_gate.validate_plan_for_agent_spawn(
        agent_name="planner",
        task_id="task-1",
        task_manager=manager,
    )

    assert cli_result["valid"] is False
    assert "inventory_unavailable" in cli_result["errors"][0]
    assert spawn_result is not None
    assert spawn_result["success"] is False
    assert "inventory_unavailable" in spawn_result["error"]
