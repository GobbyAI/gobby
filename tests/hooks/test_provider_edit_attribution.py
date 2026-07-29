"""Provider-contract coverage for structured file mutation normalization."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.grok import GrokAdapter
from gobby.hooks.normalization import normalize_tool_fields

pytestmark = pytest.mark.unit

FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures" / "provider_contracts" / "file-mutation-hooks.jsonl"
)
WORKSPACE_ROOT = Path(__file__).parents[2].resolve()


def _contract_records() -> list[dict[str, Any]]:
    fixture_text = FIXTURE_PATH.read_text(encoding="utf-8").replace(
        "<WORKSPACE>",
        str(WORKSPACE_ROOT),
    )
    return [json.loads(line) for line in fixture_text.splitlines() if line.strip()]


@pytest.mark.parametrize(
    "record",
    _contract_records(),
    ids=lambda record: f"{record['provider']}-{record['event']}",
)
def test_provider_mutation_contracts_produce_ordered_canonical_paths(
    record: dict[str, Any],
) -> None:
    data = deepcopy(record["payload"])
    if record["provider"] == "grok":
        data = GrokAdapter().translate_to_hook_event(data).data
    else:
        normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_structured_mutation"] is True
    assert data["canonical_file_paths"] == record["expected_paths"]


def test_structured_mutation_without_path_preserves_empty_canonical_sentinel() -> None:
    data: dict[str, Any] = {
        "tool_name": "WriteFile",
        "tool_input": {"content": "path unavailable"},
    }

    normalize_tool_fields(data)

    assert data["canonical_file_paths"] == []
    assert data["canonical_structured_mutation"] is True


@pytest.mark.parametrize("leaf_name", ["create", "edit", "replace"])
def test_generic_mcp_leaf_names_are_not_file_mutations(leaf_name: str) -> None:
    data: dict[str, Any] = {
        "tool_name": f"mcp__service__{leaf_name}",
        "mcp_server": "service",
        "mcp_tool": leaf_name,
        "tool_input": {"path": "src/example.py"},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "mcp"
    assert "canonical_repo_mutation" not in data
    assert "canonical_structured_mutation" not in data


def test_file_content_with_patch_markers_does_not_add_phantom_paths() -> None:
    data: dict[str, Any] = {
        "tool_name": "WriteFile",
        "tool_input": {
            "file_path": "docs/example.md",
            "content": "*** Update File: src/phantom.py\n",
        },
    }

    normalize_tool_fields(data)

    assert data["canonical_file_paths"] == ["docs/example.md"]


def test_change_lists_preserve_old_new_and_direct_paths_once() -> None:
    data = {
        "tool_name": "Edit",
        "tool_input": {
            "changes": [
                {"old_path": "src/old.py", "newPath": "src/new.py"},
                {"filePath": "src/old.py"},
                {"target_path": "docs/plan.md"},
            ]
        },
        "tool_response": {"changes": [{"path": "src/new.py"}, {"file_path": "docs/plan.md"}]},
    }

    normalize_tool_fields(data)

    assert data["canonical_file_paths"] == [
        "src/old.py",
        "src/new.py",
        "docs/plan.md",
    ]
