"""The c7 poller records each sample of the owned index and the first partial state it observes."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import psycopg
import pytest
from c0_poller import IndexPoller, clean_file_count, corpus_project_id
from runtime_boundary import EvidenceError


def _poller(tmp_path: Path, count: int | None = None, *, clean: int = 9) -> IndexPoller:
    def read_count() -> int:
        if count is None:
            raise psycopg.OperationalError("owned database unreachable")
        return count

    return IndexPoller(
        count=read_count,
        clock=time.monotonic,
        clean=clean,
        log=tmp_path / "poll.jsonl",
        interval=0.001,
    )


def _run_once(poller: IndexPoller) -> list[dict[str, object]]:
    poller.start()
    poller.stop()
    rows = poller.log.read_text(encoding="utf-8").splitlines()
    return [json.loads(row) for row in rows]


def test_a_partial_count_is_the_first_qualifying_observation_and_is_logged(tmp_path: Path) -> None:
    poller = _poller(tmp_path, count=4)

    rows = _run_once(poller)

    assert poller.first_qualifying == 4
    assert rows[0]["count"] == 4
    assert all(isinstance(row["t_ms"], int) for row in rows)


@pytest.mark.parametrize("count", [0, 9, 12])
def test_an_empty_or_complete_count_never_qualifies(tmp_path: Path, count: int) -> None:
    poller = _poller(tmp_path, count=count)

    _run_once(poller)

    assert poller.first_qualifying is None


def test_a_failed_count_is_logged_as_an_error_row_and_fails_the_poller(tmp_path: Path) -> None:
    poller = _poller(tmp_path, count=None)

    poller.start()
    with pytest.raises(EvidenceError, match="index poller failed"):
        poller.stop()
    rows = [json.loads(row) for row in poller.log.read_text(encoding="utf-8").splitlines()]

    assert rows
    assert all("error" in row and "count" not in row for row in rows)
    assert poller.first_qualifying is None


def test_a_poller_cannot_start_twice_or_stop_before_it_starts(tmp_path: Path) -> None:
    poller = _poller(tmp_path, count=4)

    with pytest.raises(EvidenceError, match="not running"):
        poller.stop()
    poller.start()
    try:
        with pytest.raises(EvidenceError, match="already started"):
            poller.start()
    finally:
        poller.stop()


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("count broke"),
        EvidenceError("index row count returned no row"),
        psycopg.OperationalError("owned database unreachable"),
    ],
)
def test_an_unexpected_sampler_failure_is_raised_by_stop_and_fails_closed(
    tmp_path: Path, failure: Exception
) -> None:
    sampled = threading.Event()
    failed = threading.Event()

    def read_count() -> int:
        if not sampled.is_set():
            sampled.set()
            return 4
        failed.set()
        raise failure

    poller = IndexPoller(
        count=read_count,
        clock=time.monotonic,
        clean=9,
        log=tmp_path / "poll.jsonl",
        interval=0.001,
    )
    poller.start()
    assert failed.wait(timeout=5)

    with pytest.raises(EvidenceError, match="index poller failed"):
        poller.stop()
    assert poller.first_qualifying is None


def test_the_clean_count_is_the_manifest_file_count_when_it_matches_its_entries(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "C1.json"
    manifest.write_text(json.dumps({"file_count": 3, "files": ["a", "b", "c"]}))

    assert clean_file_count(manifest) == 3


def test_a_clean_count_that_disagrees_with_its_entries_is_refused(tmp_path: Path) -> None:
    manifest = tmp_path / "C1.json"
    manifest.write_text(json.dumps({"file_count": 4, "files": ["a", "b", "c"]}))

    with pytest.raises(EvidenceError, match="does not match its entries"):
        clean_file_count(manifest)


def test_the_project_id_is_read_from_the_corpus_identity_file_and_never_assumed(
    tmp_path: Path,
) -> None:
    identity = tmp_path / "corpora/gcode/C0/.gobby"
    identity.mkdir(parents=True)
    (identity / "project.json").write_text(json.dumps({"id": "project-1"}))

    assert corpus_project_id(tmp_path, "corpora/gcode/C0") == "project-1"

    (identity / "project.json").write_text(json.dumps({"name": "no id"}))
    with pytest.raises(EvidenceError, match="has no id"):
        corpus_project_id(tmp_path, "corpora/gcode/C0")
