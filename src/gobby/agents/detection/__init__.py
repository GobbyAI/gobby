"""Manifest-driven agent pane detection."""

from gobby.agents.detection.matcher import (
    CompiledManifest,
    DetectionMatch,
    MatchEvaluation,
    compile_manifest,
)
from gobby.agents.detection.schema import DetectionManifest, DetectionRule, load_manifest

__all__ = [
    "CompiledManifest",
    "DetectionManifest",
    "DetectionMatch",
    "DetectionRule",
    "MatchEvaluation",
    "compile_manifest",
    "load_manifest",
]
