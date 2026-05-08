from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gobby.plans.coverage_manifest import EmptyComponentError, _sanitize, coverage_manifest_path

pytestmark = pytest.mark.unit


def test_canonical_form(tmp_path: Path) -> None:
    assert (
        coverage_manifest_path(
            tmp_path,
            project_id="project",
            root_task_ref="#12725",
            plan_id="task-13175-plan",
        )
        == tmp_path / ".gobby/plans/coverage/project/12725/task-13175-plan.coverage.yaml"
    )


def test_replaces_disallowed_chars() -> None:
    assert _sanitize("abc/def ghi") == "abc-def-ghi"


def test_replaces_slash_path_traversal() -> None:
    assert _sanitize("../abc/def") == "abc-def"


def test_rejects_empty_post_sanitize() -> None:
    with pytest.raises(EmptyComponentError):
        _sanitize("///...")


def test_drops_leading_hash() -> None:
    assert _sanitize("#12725", kind="root_task_ref") == "12725"


def test_strips_punct() -> None:
    assert _sanitize("-._abc_.-") == "abc"


def test_truncate_with_hash() -> None:
    raw = "a" * 80
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:7]
    assert _sanitize(raw) == f"{'a' * 56}-{suffix}"


def test_truncate_hash_uses_pre_replacement_input() -> None:
    left = "a" * 70 + "!"
    right = "a" * 70 + "?"
    assert _sanitize(left) != _sanitize(right)


def test_truncate_hash_disambiguates_collisions() -> None:
    left = "x" * 65
    right = "x" * 64 + "y"
    assert _sanitize(left) != _sanitize(right)


def test_distinct_raws_can_collapse_before_identity_check() -> None:
    assert _sanitize("a/b") == _sanitize("a b")


def test_case_collision_paths_remain_distinct_but_casefold_equal(tmp_path: Path) -> None:
    upper = coverage_manifest_path(
        tmp_path, project_id="Project", root_task_ref="#1", plan_id="Plan"
    )
    lower = coverage_manifest_path(
        tmp_path, project_id="project", root_task_ref="#1", plan_id="plan"
    )

    assert upper != lower
    assert str(upper).casefold() == str(lower).casefold()


def test_windows_reserved_disambiguated() -> None:
    assert _sanitize("CON") == "CON_"
    assert _sanitize("con") == "con_"
    assert _sanitize("Lpt9") == "Lpt9_"
