"""Pins the Python pane matcher to the corpus the Rust engine also checks (#24381)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gobby.agents.detection.matcher import compile_manifest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = REPO_ROOT / "crates/gdaemon/tests/fixtures/pane_detection/corpus.json"
BUNDLED_DIR = REPO_ROOT / "src/gobby/install/shared/detection"
RECORD_ENV = "GOBBY_RECORD_PANE_DETECTION_CORPUS"


def _content(case: dict[str, Any]) -> str:
    if "provider" in case:
        return (BUNDLED_DIR / f"{case['provider']}.toml").read_text(encoding="utf-8")
    if "manifest" in case:
        return (REPO_ROOT / str(case["manifest"])).read_text(encoding="utf-8")
    return str(case["content"])


def _refusal_message(exc: ValidationError) -> str:
    error = exc.errors()[0]
    ctx = error.get("ctx", {})
    if "error" in ctx:
        return str(ctx["error"])
    return str(error["msg"])


def _observe(case: dict[str, Any]) -> dict[str, Any]:
    try:
        compiled = compile_manifest(_content(case))
    except ValidationError as exc:
        return {"refusal": _refusal_message(exc)}
    evaluation = compiled.match(case["pane"])
    match = evaluation.match
    return {
        "state": match.state if match is not None else None,
        "rule_id": match.rule_id if match is not None else None,
        "reason": match.reason if match is not None else None,
        "issues": [
            {"rule_id": issue.rule_id, "code": issue.code, "pattern": issue.pattern}
            for issue in evaluation.issues
        ],
    }


def test_matcher_cases_match_python() -> None:
    document = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = document["cases"]
    if os.environ.get(RECORD_ENV) == "1":
        for case in cases:
            case["expect"] = _observe(case)
        CORPUS_PATH.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return
    for case in cases:
        assert _observe(case) == case["expect"], case["id"]
