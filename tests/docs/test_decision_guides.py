"""Decision capability guide rows (plan decision-classifier-path, section 4.1)."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

CONFIGURATION_REQUIRED = [
    "openrouter-decisions",
    "workers-ai",
    "lms load",
    "systemone",
    "clef_mlx.py serve",
    "backend_max_state_tokens",
    "0.4.26 Build 4",
    "/v1/systemone",
    "$secret:",
    "127.0.0.1",
    "16384",
    "GET /api/llm/status",
    "docs/evidence/decisions/wire-contracts.md",
]

FEATURES_REQUIRED = [
    "found_work",
    "hybrid_fallback",
    "shadow",
    "enforce",
    "~/.gobby/decisions/shadow",
    "scripts/decisions_eval.py",
]

FEATURES_CONSUMER_PATHS = ["ai.decisions.tool_rerank", "ai.decisions.found_work"]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _table_rows_with(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("|") and needle in line]


def test_configuration_guide_documents_ai_decisions_contract() -> None:
    configuration = _read("docs/guides/configuration.md")

    assert re.search(r"^#{2,4} .*`ai\.decisions`", configuration, re.MULTILINE)
    missing = [needle for needle in CONFIGURATION_REQUIRED if needle not in configuration]
    assert not missing, f"configuration.md is missing: {missing}"


def test_features_guide_lists_both_decision_consumers_with_modes_and_fallbacks() -> None:
    features = _read("docs/guides/llm-features.md")

    missing = [needle for needle in FEATURES_REQUIRED if needle not in features]
    assert not missing, f"llm-features.md is missing: {missing}"
    for config_path in FEATURES_CONSUMER_PATHS:
        assert _table_rows_with(features, config_path), f"no table row lists {config_path}"


def test_mcp_tools_guide_lists_gobby_decisions_registry_row() -> None:
    mcp_tools = _read("docs/guides/mcp-tools.md")

    rows = _table_rows_with(mcp_tools, "`gobby-decisions`")
    assert rows, "mcp-tools.md has no internal registry row for gobby-decisions"
    assert "evaluate" in rows[0]
