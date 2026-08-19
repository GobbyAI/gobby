"""Importable spawn target for multi-daemon config workers.

Nested pytest conftest modules are loaded as ``conftest``, so a
``multiprocessing`` spawn child cannot unpickle ``conftest._worker_entry``
or ``conftest.WorkerSpec``. Keep those objects in this regular module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    name: str
    dsn: str
    home: Path
    passphrase: str


def _worker_entry(connection: Connection, spec: WorkerSpec) -> None:
    from tests.integration.config.conftest import _serve

    asyncio.run(_serve(connection, spec))
