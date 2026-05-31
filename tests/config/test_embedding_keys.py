from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "gobby"
ALLOWED_LITERAL_FILES = {
    SRC_ROOT / "config" / "embedding_keys.py",
}
EMBEDDING_CONFIG_KEY_PATTERN = re.compile(
    r"(?:^|[^A-Za-z0-9_])(?:ai\.)?embeddings\."
    r"(?:api_base|api_key|model|dim|query_prefix|provider)(?:$|[^A-Za-z0-9_])"
)


def test_embedding_keys_centralized_and_guarded() -> None:
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        if path in ALLOWED_LITERAL_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if EMBEDDING_CONFIG_KEY_PATTERN.search(node.value):
                    offenders.append(f"{path.relative_to(SRC_ROOT)}:{node.lineno}")

    assert offenders == [], f"Embedding config key literals outside central module: {offenders}"
