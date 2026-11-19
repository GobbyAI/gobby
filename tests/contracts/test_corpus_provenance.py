"""Cross-layer provenance: platform records, synthetic-only corpora and README commands.

This module reads the committed manifests, case files, vector files and READMEs of
the rules, adapters and pipeline layers directly. It imports no layer loader, so every
check runs against the files as committed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

CONTRACTS_DIR = Path(__file__).resolve().parent
PLATFORM_KEYS = frozenset({"macos", "linux", "windows-x64", "windows-arm64"})
RECORD_COMMAND = "GOBBY_CORPUS_RECORD=1"
PATH_MARKERS = ("/Users/", "/home/", "/private/var/", "C:\\Users\\", "/transcripts/")

# Minimum committed case counts. Rules: one case for each required scenario ID (92).
# Adapters: one inbound and one outbound case for each of the five adapters (10), four
# node-origin cases, two git-source verdicts and two route refusals (18). Pipeline:
# four session-start cases, a node session with a missing row, a deferred activation,
# a blocking webhook and a rule block that short-circuits the handler (8).
LAYERS: dict[str, dict[str, Any]] = {
    "rules": {
        "dir": CONTRACTS_DIR / "rules",
        "test": "tests/contracts/test_rules_corpus.py",
        "min_cases": 92,
        "vectors": "normalize_vectors.json",
    },
    "adapters": {
        "dir": CONTRACTS_DIR / "adapters",
        "test": "tests/contracts/test_adapters_corpus.py",
        "min_cases": 18,
        "vectors": None,
    },
    "pipeline": {
        "dir": CONTRACTS_DIR / "pipeline",
        "test": "tests/contracts/test_pipeline_corpus.py",
        "min_cases": 8,
        "vectors": None,
    },
}
LAYER_NAMES = tuple(LAYERS)


def _layer_cases(layer: str) -> list[dict[str, Any]]:
    layer_dir = LAYERS[layer]["dir"]
    case_files = sorted((layer_dir / "cases").glob("*.json"))
    assert case_files, f"no case files under {layer}/cases"
    return [case for path in case_files for case in json.loads(path.read_text())["cases"]]


@pytest.mark.parametrize("layer", LAYER_NAMES)
def test_layer_manifests_carry_platform_record(layer: str) -> None:
    manifest_path = LAYERS[layer]["dir"] / "manifest.json"
    assert manifest_path.is_file(), f"missing {layer}/manifest.json"
    record = json.loads(manifest_path.read_text()).get("platform_record")
    assert isinstance(record, dict), f"{layer}/manifest.json has no platform_record"
    assert set(record) == PLATFORM_KEYS, f"{layer} platform_record keys: {sorted(record)}"
    vectors_name = LAYERS[layer]["vectors"]
    if vectors_name is None:
        return
    vectors_path = LAYERS[layer]["dir"] / vectors_name
    assert vectors_path.is_file(), f"missing {layer}/{vectors_name}"
    vector_record = json.loads(vectors_path.read_text()).get("platform_record")
    assert isinstance(vector_record, dict), f"{layer}/{vectors_name} has no platform_record"
    assert set(vector_record) == PLATFORM_KEYS, f"{layer} vector platform_record keys differ"


@pytest.mark.parametrize("layer", LAYER_NAMES)
def test_committed_corpus_is_synthetic(layer: str) -> None:
    cases = _layer_cases(layer)
    minimum = LAYERS[layer]["min_cases"]
    assert len(cases) >= minimum, f"{layer} holds {len(cases)} cases; at least {minimum} required"
    not_synthetic = [case.get("id") for case in cases if case.get("provenance") != "synthetic"]
    assert not not_synthetic, f"{layer} cases without synthetic provenance: {not_synthetic}"
    for path in sorted(LAYERS[layer]["dir"].rglob("*.json")):
        text = path.read_text()
        markers = [marker for marker in PATH_MARKERS if marker in text]
        assert not markers, f"{path.name} holds absolute or transcript path markers: {markers}"


@pytest.mark.parametrize("layer", LAYER_NAMES)
def test_layer_readmes_name_commands(layer: str) -> None:
    readme = LAYERS[layer]["dir"] / "README.md"
    assert readme.is_file(), f"missing {layer}/README.md"
    text = readme.read_text()
    for marker in (RECORD_COMMAND, LAYERS[layer]["test"], "re-record"):
        assert marker in text, f"{layer}/README.md does not name {marker!r}"
