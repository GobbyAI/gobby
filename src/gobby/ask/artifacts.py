"""Run-owned, content-addressed Ask bodies retained in PostgreSQL."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import psycopg

from gobby.paths import get_gobby_home
from gobby.storage.hub.operation_deadline import database_operation_deadline
from gobby.storage.hub.protocol import HubDatabase


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class AskArtifactStore:
    """Immutable bodies; filesystem paths are reserved for ephemeral runtime credentials."""

    def __init__(
        self, state_root: Path | None, project_id: str, run_id: str, *, db: HubDatabase
    ) -> None:
        self.db = db
        self.project_id = project_id
        self.run_id = run_id
        self.run_root = (state_root or get_gobby_home()) / "ask-runtime" / project_id / run_id

    def write_body(
        self, kind: str, body: dict[str, Any], *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise TimeoutError("Ask artifact deadline exceeded")
        if not kind or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in kind):
            raise ValueError("artifact kind must be a non-empty safe identifier")
        payload = _canonical_json(body)
        digest = hashlib.sha256(payload).hexdigest()
        deadline = (
            database_operation_deadline(
                timeout_seconds=timeout_seconds, operation_timeout_seconds=timeout_seconds
            )
            if timeout_seconds is not None
            else nullcontext()
        )
        try:
            with deadline:
                with self.db.transaction() as conn:
                    owner = conn.execute(
                        "SELECT id FROM pipeline_executions WHERE id = %s AND project_id = %s "
                        "AND pipeline_name = 'native-ask' FOR KEY SHARE",
                        (self.run_id, self.project_id),
                    ).fetchone()
                    if owner is None:
                        raise ValueError("Ask artifact owner does not exist in this project")
                    conn.execute(
                        "INSERT INTO ask_artifacts (execution_id, kind, sha256, body) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (execution_id, kind, sha256) DO NOTHING",
                        (self.run_id, kind, digest, payload.decode()),
                    )
                pointer = {
                    "kind": kind,
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                    "sha256": digest,
                    "size_bytes": len(payload),
                }
                if self.read_body(pointer) != body:
                    raise RuntimeError("immutable Ask artifact collision")
                return pointer
        except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as error:
            if timeout_seconds is None:
                raise
            raise TimeoutError("Ask artifact deadline exceeded") from error

    def read_body(self, pointer: dict[str, Any]) -> dict[str, Any]:
        if pointer.get("project_id") != self.project_id or pointer.get("run_id") != self.run_id:
            raise ValueError("Ask artifact pointer does not belong to this project and run")
        row = self.db.fetchone(
            "SELECT a.body FROM ask_artifacts a JOIN pipeline_executions e "
            "ON e.id = a.execution_id WHERE a.execution_id = %s AND e.project_id = %s "
            "AND a.kind = %s AND a.sha256 = %s",
            (self.run_id, self.project_id, pointer.get("kind"), pointer.get("sha256")),
        )
        if row is None:
            raise ValueError("Ask artifact not found")
        payload = str(row["body"]).encode()
        if hashlib.sha256(payload).hexdigest() != pointer.get("sha256"):
            raise RuntimeError("Ask artifact hash mismatch")
        if len(payload) != pointer.get("size_bytes"):
            raise RuntimeError("Ask artifact size mismatch")
        body = json.loads(payload)
        if not isinstance(body, dict):
            raise RuntimeError("Ask artifact body must be an object")
        return body

    def verify_manifest(self) -> None:
        for row in self.db.fetchall(
            "SELECT a.kind, a.sha256, octet_length(a.body) AS size_bytes FROM ask_artifacts a "
            "JOIN pipeline_executions e ON e.id = a.execution_id "
            "WHERE a.execution_id = %s AND e.project_id = %s",
            (self.run_id, self.project_id),
        ):
            self.read_body({**dict(row), "run_id": self.run_id, "project_id": self.project_id})
