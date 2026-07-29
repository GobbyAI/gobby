from __future__ import annotations

import pytest

from gobby.memory.dream.options import (
    DreamRunOptions,
    dream_scope_key,
    normalize_dream_options,
)


def test_normalize_dream_options_preserves_full_option_shape() -> None:
    options = DreamRunOptions(
        dry_run=True,
        skip_consolidation=True,
        memory_type="fact",
        project_id="project-1",
        include_global=False,
        full_sweep=True,
    ).to_dict()

    assert normalize_dream_options(options) == options


def test_normalize_dream_options_fills_defaults() -> None:
    assert normalize_dream_options({}) == {
        "dry_run": False,
        "skip_consolidation": False,
        "memory_type": None,
        "project_id": None,
        "global_only": False,
        "include_global": None,
        "full_sweep": False,
    }


def test_normalize_dream_options_accepts_aggregate_shape() -> None:
    assert normalize_dream_options(
        {
            "aggregate": True,
            "dry_run": True,
            "skip_consolidation": True,
            "include_global": True,
            "full_sweep": True,
        }
    ) == {
        "dry_run": True,
        "skip_consolidation": True,
        "memory_type": None,
        "project_id": None,
        "global_only": False,
        "include_global": True,
        "full_sweep": True,
    }


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"global_only": True, "project_id": "ignored"}, "global"),
        ({"aggregate": True}, "all"),
        ({"project_id": "project-1"}, "project:project-1"),
    ],
)
def test_dream_scope_key(options: dict[str, object], expected: str) -> None:
    assert dream_scope_key(options) == expected
