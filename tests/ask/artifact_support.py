"""In-memory artifact boundary for pure answer-validation tests."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from gobby.ask.artifacts import AskArtifactStore, _canonical_json


class MemoryArtifacts(AskArtifactStore):
    def __init__(self, state_root: Path, project_id: str, run_id: str) -> None:
        self.project_id = project_id
        self.run_id = run_id
        self.run_root = state_root / project_id / run_id
        self.bodies: dict[str, bytes] = {}
        self.lock = threading.Lock()

    def write_body(
        self, kind: str, body: dict[str, Any], *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        payload = _canonical_json(body)
        digest = hashlib.sha256(payload).hexdigest()
        with self.lock:
            self.bodies[digest] = payload
        return {
            "kind": kind,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "sha256": digest,
            "size_bytes": len(payload),
        }

    def read_body(self, pointer: dict[str, Any]) -> dict[str, Any]:
        payload = self.bodies[pointer["sha256"]]
        return dict(json.loads(payload))
