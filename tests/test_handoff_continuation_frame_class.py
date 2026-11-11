"""A registration timeout records only the class of the seat's last frame."""

from __future__ import annotations

import pytest

from gobby.agents.detection.matcher import CompiledManifest, compile_manifest
from gobby.agents.idle_detector import IdleDetector
from tests.agents.detection_test_support import BundledDetectionRegistry, detection_manifest
from tests.e2e.handoff_continuation_runner import classify_frame

pytestmark = pytest.mark.unit

_SECRET = "provider-secret-not-for-artifacts"


class _Synthetic:
    def __init__(self, manifest: CompiledManifest) -> None:
        self.manifest = manifest

    def for_provider(self, provider_id: str) -> CompiledManifest | None:
        return self.manifest


def test_trust_prompt_frame_is_named_without_its_text() -> None:
    manifest = compile_manifest(detection_manifest("probe", "PROBE"))
    detector = IdleDetector(_Synthetic(manifest), "probe")
    frame = f"\x1b[1m{_SECRET}\x1b[0m\nPROBE trust this folder?\n"
    assert classify_frame(manifest, detector, frame) == "trust_prompt"


def test_frame_without_a_prompt_or_composer_is_unknown() -> None:
    manifest = compile_manifest(detection_manifest("probe", "PROBE"))
    detector = IdleDetector(_Synthetic(manifest), "probe")
    assert classify_frame(manifest, detector, f"{_SECRET}\nloading\n") == "unknown"
    assert classify_frame(None, detector, f"{_SECRET}\n") == "unknown"


def test_a_visible_composer_is_named_composer() -> None:
    registry = BundledDetectionRegistry()
    detector = IdleDetector(registry, "claude")
    frame = "Welcome\n\n────────\n❯ \n────────\n"
    assert detector.composer_read(frame).state != "unknown", "fixture must show a composer"
    assert classify_frame(None, detector, frame) == "composer"
