"""Authenticate an original RED job's retained full output at close time.

Compaction can trim the failure bodies a test run delivered into the transcript.
The provider's ``tasks/<job_id>.output`` file or the RTK recall row named by the
compacted output still holds them. Gate 12 may read that artifact only while it
provably is the original job's output: named by the job, settled no later than
its completion, untouched before the production edit that closes its window,
unchanged while it is read, and containing what the transcript delivered.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import zlib
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta

from gobby.integrations.rtk import platform_paths
from gobby.tasks.transcript_background import (
    background_job_receipt,
    output_as_of,
    recovered_deliveries,
)
from gobby.tasks.transcript_evidence_models import TranscriptValidationRun

#: Claude writes the job file's ``[exited with code N]`` trailer just after it
#: emits the terminal notice (279ms on #23879), so the file settles slightly later.
RETAINED_OUTPUT_FLUSH_GRACE = timedelta(seconds=1)
_MAX_RETAINED_BYTES = 8 * 1024 * 1024
_RECALL_HINT_RE = re.compile(r"(?m)^[ \t]*\[full output: rtk recall ([0-9a-f]{12,64})\][ \t]*\r?$")
# Lines the provider or RTK writes around the command's own output.
_GENERATED_LINE_RE = re.compile(
    r"\[full output: rtk recall [0-9a-f]+\]|\[exited with code -?\d+\]|Exit code -?\d+"
    r"|Pytest: .*|Failures:|\d+\. \[FAIL\] \S+|… \+\d+ more failures?"
)


def retained_red_output(run: TranscriptValidationRun, production_at: datetime) -> str | None:
    """The run's retained full output, or None when any provenance check fails.

    Only output the transcript delivered before ``production_at`` is matched; a
    later Read, tail or Monitor of the job's own file counts for nothing. Every
    recovered delivery before the cutoff must appear in it; a run with none checks
    its as-of output.
    """
    as_of = output_as_of(run, production_at)
    if as_of.outcome != "failure" or not as_of.output:
        return None
    if run.background_output_path is not None:
        retained = _job_output(run, production_at)
    elif run.output_recovered_from is None:
        retained = _recall_artifact(run, production_at)
    else:
        return None
    if retained is None:
        return None
    pieces = [(d.text or "", d.truncated) for d in recovered_deliveries(run, production_at)]
    for text, truncated in pieces or [(as_of.output, as_of.output_truncated)]:
        if not _contains_delivered(text, truncated, retained):
            return None
    return retained


def delivered_proof_runs(
    run: TranscriptValidationRun, production_at: datetime | None
) -> list[TranscriptValidationRun]:
    """Each pre-cutoff delivery proved alone, then the authenticated full output.

    Deliveries are never joined: a newline between two reads would make unrelated
    lines adjacent to a failure section. Without a production cutoff there is no
    retained output to authenticate, so only the deliveries are returned.
    """
    runs = [
        replace(run, output=delivery.text, output_truncated=delivery.truncated)
        for delivery in recovered_deliveries(run, production_at)
    ]
    retained = retained_red_output(run, production_at) if production_at is not None else None
    if retained is not None:
        runs.append(replace(run, output=retained))
    return runs


def _job_output(run: TranscriptValidationRun, production_at: datetime) -> str | None:
    path = run.background_output_path
    if path is None or run.background_job_id is None or run.exit_code is None:
        return None
    parent, name = os.path.split(path)
    if not os.path.isabs(path) or (os.path.basename(parent), name) != (
        "tasks",
        f"{run.background_job_id}.output",
    ):
        return None
    try:
        # O_NONBLOCK: opening a FIFO planted at the path must not wait for a writer.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_RETAINED_BYTES:
                return None
            data = handle.read(_MAX_RETAINED_BYTES + 1)
            info = os.fstat(handle.fileno())
    except OSError:
        return None
    # A write between the snapshot and the read would credit bytes the checks never saw.
    if _settled(before) != _settled(info):
        return None
    # os.utime can set mtime back but not ctime, so a backdated rewrite still shows late.
    modified = max(info.st_mtime_ns, info.st_ctime_ns) / 1_000_000_000
    if (
        len(data) > _MAX_RETAINED_BYTES
        or modified > (run.completed_at + RETAINED_OUTPUT_FLUSH_GRACE).timestamp()
        or modified >= production_at.timestamp()
    ):
        return None
    text = data.decode("utf-8", errors="replace")
    lines = text.rstrip().splitlines()
    if not lines or lines[-1].strip() != f"[exited with code {run.exit_code}]":
        return None
    return text


def _settled(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _recall_artifact(run: TranscriptValidationRun, production_at: datetime) -> str | None:
    recalls = set(_RECALL_HINT_RE.findall(run.output or ""))
    if len(recalls) != 1:
        return None
    database = platform_paths().data_dir / "recall.db"
    try:
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT exit_code, created_at, truncated, codec, blob FROM recall WHERE hash = ?",
                (recalls.pop(),),
            ).fetchone()
    except (sqlite3.Error, ValueError):
        return None
    if row is None:
        return None
    exit_code, created_at, truncated, codec, blob = row
    if (
        truncated
        or codec != "gzip"
        or not isinstance(blob, bytes)
        or not isinstance(created_at, int)
        or (exit_code is not None and exit_code != run.exit_code)
        or created_at < int(run.started_at.timestamp())
        or created_at > run.completed_at.timestamp()
        or created_at >= production_at.timestamp()
    ):
        return None
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = decoder.decompress(blob, _MAX_RETAINED_BYTES + 1)
    except zlib.error:
        return None
    if len(data) > _MAX_RETAINED_BYTES or decoder.unconsumed_tail or not decoder.eof:
        return None
    return data.decode("utf-8", errors="replace")


def _contains_delivered(delivered: str, truncated: bool, retained: str) -> bool:
    """Every line one delivery carried appears in the retained output."""
    lines = delivered.splitlines()
    receipt = background_job_receipt(delivered)
    if receipt is not None:
        # The handoff line, then the job id Claude records beside it.
        lines = lines[1:]
        if lines and lines[0].strip() == receipt["job"]:
            lines = lines[1:]
    if lines and truncated:
        lines = lines[:-1]
    available = [line.strip() for line in retained.splitlines()]
    exact = set(available)
    verified = False
    for raw in lines:
        line = raw.strip()
        if not line or _GENERATED_LINE_RE.fullmatch(line):
            continue
        # Compacted output elides a long line's tail with "..." or "…".
        prefix = line.removesuffix("...").removesuffix("…").rstrip()
        if prefix != line:
            if not any(item.startswith(prefix) for item in available):
                return False
        elif line not in exact:
            return False
        verified = True
    return verified


__all__ = ["RETAINED_OUTPUT_FLUSH_GRACE", "delivered_proof_runs", "retained_red_output"]
