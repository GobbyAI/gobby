"""Shared manifest helpers for detector integration tests."""

from __future__ import annotations

from pathlib import Path

from gobby.agents.detection.matcher import CompiledManifest, compile_manifest
from gobby.agents.detection.registry import save_user_detection_manifest
from gobby.storage.hub.protocol import HubDatabase


class BundledDetectionRegistry:
    """Small immutable registry for detector unit tests."""

    def __init__(self) -> None:
        manifest_dir = Path(__file__).parents[2] / "src/gobby/install/shared/detection"
        self._manifests = {
            path.stem: compile_manifest(path.read_bytes()) for path in manifest_dir.glob("*.toml")
        }

    def for_provider(self, provider_id: str) -> CompiledManifest | None:
        return self._manifests.get(provider_id.strip().lower())


def detection_manifest(provider: str, marker: str) -> str:
    return f'''\
id = "{provider}"
version = "1"
engine = 1

[[rules]]
id = "trust_prompt"
state = "blocked"
reason = "trust"
priority = 1000
region = "whole_recent"
contains = ["{marker} trust"]

[[rules]]
id = "provider_error"
state = "stall"
reason = "provider_error"
priority = 850
region = "whole_recent"
contains = ["{marker} unavailable"]

[[rules]]
id = "idle_prompt"
state = "idle"
priority = 100
region = "whole_recent"
contains = ["{marker} idle"]
'''


def replace_detection_manifest(db: HubDatabase, provider: str, marker: str) -> None:
    save_user_detection_manifest(db, detection_manifest(provider, marker))
