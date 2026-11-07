"""C0's c7 poller: samples the owned index's file-state count while the cold index runs."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import psycopg
from provision_environment import PORTS
from runtime_boundary import EvidenceError, contained_file, require
from validate_environment import _load_env_file

POLL_INTERVAL_SECONDS = 0.2
INDEX_ROWS_QUERY = "SELECT count(*) FROM code_indexed_file_states WHERE project_id = %s"


class IndexPoller:
    """Samples the count on a background thread and logs every sample to a new file."""

    def __init__(
        self,
        *,
        count: Callable[[], int],
        clock: Callable[[], float],
        clean: int,
        log: Path,
        interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        require(clean > 0, "clean file count must be positive")
        self._count = count
        self._clock = clock
        self._interval = interval
        self.clean = clean
        self.log = log
        self.first_qualifying: int | None = None
        self._origin = 0.0
        self._handle: TextIO | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: Exception | None = None

    def start(self) -> None:
        require(self._thread is None, "poller already started")
        descriptor = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._handle = os.fdopen(descriptor, "w", encoding="utf-8")
        self._origin = self._clock()
        self._stop = threading.Event()
        self._failure = None
        self._thread = threading.Thread(
            target=self._run, args=(self._handle, self._stop), name="c7-index-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        require(self._thread is not None and self._handle is not None, "poller is not running")
        self._stop.set()
        self._thread.join()
        self._handle.close()
        failure = self._failure
        self._thread = None
        self._handle = None
        if failure is not None:
            raise EvidenceError(
                f"index poller failed: {type(failure).__name__}: {failure}"
            ) from failure

    def _run(self, handle: TextIO, stop: threading.Event) -> None:
        # Sample first, then wait: a run always records at least one row.
        try:
            while True:
                self._sample(handle)
                if stop.wait(self._interval):
                    return
        except Exception as error:
            # The sampler owns its failures: withdraw any qualifying sample so C7 fails closed,
            # and keep the failure for stop() to raise, so the thread cannot end silently.
            self.first_qualifying = None
            self._failure = error

    def _sample(self, handle: TextIO) -> None:
        elapsed_ms = int((self._clock() - self._origin) * 1000)
        row: dict[str, Any]
        try:
            count = self._count()
        except psycopg.Error as error:
            # A failed read is evidence in the log, not a crash of the cold index.
            row = {"t_ms": elapsed_ms, "error": f"{type(error).__name__}: {error}"}
        else:
            row = {"t_ms": elapsed_ms, "count": count}
            if self.first_qualifying is None and 0 < count < self.clean:
                self.first_qualifying = count
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()


def index_row_count(root: Path, project_id: str) -> Callable[[], int]:
    """A read-only count of the owned index's file-state rows for one project."""
    services = _load_env_file(contained_file(root, "config/services.env"))

    def count() -> int:
        with psycopg.connect(
            host="127.0.0.1",
            port=PORTS["postgres"],
            dbname=services["BAKEOFF_POSTGRES_DB"],
            user=services["BAKEOFF_POSTGRES_USER"],
            password=services["BAKEOFF_POSTGRES_PASSWORD"],
            connect_timeout=5,
            options="-c default_transaction_read_only=on",
        ) as conn:
            row = conn.execute(INDEX_ROWS_QUERY, (project_id,)).fetchone()
        if row is None:
            raise EvidenceError("index row count returned no row")
        return int(row[0])

    return count


def corpus_project_id(root: Path, corpus_relative: str) -> str:
    """The project id in the workspace's own identity file, as launch_services reads it."""
    identity = json.loads(
        contained_file(root, f"{corpus_relative}/.gobby/project.json").read_text(encoding="utf-8")
    )
    project_id = identity.get("id")
    require(isinstance(project_id, str) and project_id != "", f"{corpus_relative} has no id")
    return project_id


def clean_file_count(manifest: Path) -> int:
    """The clean C1 count: its file_count, which must equal its own entry list."""
    corpus = json.loads(manifest.read_text(encoding="utf-8"))
    files = corpus.get("files")
    count = corpus.get("file_count")
    require(
        isinstance(files, list) and isinstance(count, int) and count > 0 and count == len(files),
        f"{manifest} file_count does not match its entries",
    )
    return count
