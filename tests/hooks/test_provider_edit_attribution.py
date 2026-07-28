"""Provider-contract coverage for structured file mutation normalization."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.grok import GrokAdapter
from gobby.hooks.normalization import normalize_tool_fields

FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures" / "provider_contracts" / "file-mutation-hooks.jsonl"
)


def _contract_records() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURE_PATH.read_text().splitlines() if line.strip()]


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
        data = GrokAdapter()._normalize_event_data(data)
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
