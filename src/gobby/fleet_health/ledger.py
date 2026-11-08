"""The admission ledger: when new seats were admitted, so the ramp cap survives restarts.

Contract: ``GOBBY_HOME/state/fleet-admissions.json`` holds ``{"admitted_at_unix": [...]}``.
Writes go through a temporary sibling and ``os.replace``, so a reader never sees a torn file.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gobby.paths import get_gobby_home


def ledger_path() -> Path:
    """The admission ledger under the Gobby home."""
    return get_gobby_home() / "state" / "fleet-admissions.json"


class _LedgerDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    admitted_at_unix: list[float] = Field(default_factory=list)


def read_admissions(path: Path) -> list[float] | None:
    """Admission times, empty when no ledger exists yet, or ``None`` when it is unreadable."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError:
        return None
    try:
        return _LedgerDocument.model_validate_json(raw).admitted_at_unix
    except ValidationError:
        return None


def write_admissions(path: Path, admitted_at_unix: list[float]) -> None:
    """Replace the ledger atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = _LedgerDocument(admitted_at_unix=admitted_at_unix).model_dump_json()
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fleet-admissions.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@contextmanager
def ledger_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock across one read-check-write of the ledger, in every process."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / f"{path.name}.lock").open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
