from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gobby.ask.artifacts import AskArtifactStore

pytestmark = pytest.mark.unit


def test_atomic_immutable_body_publication_survives_faults_and_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AskArtifactStore(tmp_path, "project", "run")
    body = {"schema_version": 1, "payload": "durable"}
    real_write = os.write
    interrupt = True

    def interrupted_write(descriptor: int, payload: bytes | memoryview) -> int:
        nonlocal interrupt
        if interrupt:
            interrupt = False
            real_write(descriptor, bytes(payload[:1]))
            raise OSError("injected short write")
        return real_write(descriptor, payload)

    monkeypatch.setattr("gobby.ask.artifacts.os.write", interrupted_write)
    with pytest.raises(OSError, match="injected short write"):
        store.write_body("snapshot", body)
    assert not list(store.bodies_root.glob("snapshot-*.json"))

    monkeypatch.setattr("gobby.ask.artifacts.os.write", real_write)
    with ThreadPoolExecutor(max_workers=8) as pool:
        pointers = list(pool.map(lambda _: store.write_body("snapshot", body), range(16)))

    assert all(pointer == pointers[0] for pointer in pointers)
    assert store.read_body(pointers[0]) == body
    assert not list(store.bodies_root.glob("*.tmp"))
    store.verify_manifest()


def test_artifact_pointer_is_bound_to_project_and_run(tmp_path: Path) -> None:
    first = AskArtifactStore(tmp_path, "project", "run-1")
    second = AskArtifactStore(tmp_path, "project", "run-2")
    pointer = first.write_body("snapshot", {"schema_version": 1})

    with pytest.raises(ValueError, match="does not belong"):
        second.read_body(pointer)
