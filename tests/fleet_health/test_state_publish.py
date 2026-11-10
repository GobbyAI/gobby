"""The state writer publishes whole files, so a reader never sees a partial document."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from gobby.fleet_health.state import fleet_state_path
from tests.fleet_health.support import write_fleet_state

pytestmark = pytest.mark.unit


def test_state_is_published_by_renaming_a_sibling_file(monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def record_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        published.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", record_replace)

    write_fleet_state(state="healthy")

    target = fleet_state_path()
    assert len(published) == 1
    staged, destination = published[0]
    assert destination == target
    assert staged.parent == target.parent
    assert staged != target
    assert not staged.exists()


def test_a_concurrent_reader_never_sees_a_partial_document() -> None:
    write_fleet_state(state="healthy")
    path = fleet_state_path()
    failures: list[str] = []
    stop = threading.Event()

    def read_until_stopped() -> None:
        while not stop.is_set():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                failures.append(str(error))
                return

    reader = threading.Thread(target=read_until_stopped)
    reader.start()
    try:
        for index in range(500):
            write_fleet_state(state="degraded", reasons=(f"load_per_core:{index}",))
    finally:
        stop.set()
        reader.join()

    assert failures == []
