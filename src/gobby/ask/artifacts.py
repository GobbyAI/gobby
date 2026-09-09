"""Owner-only immutable artifact storage for Ask runs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from gobby.paths import get_gobby_home
from gobby.utils.durable_file import durable_replace, exclusive_file_lock


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class AskArtifactStore:
    """Content-addressed Ask artifacts with one atomically published manifest."""

    def __init__(self, state_root: Path | None, project_id: str, run_id: str) -> None:
        root = state_root or get_gobby_home()
        self.project_id = project_id
        self.run_id = run_id
        self.run_root = root / "ask" / project_id / run_id
        self.bodies_root = self.run_root / "bodies"
        self.manifest_path = self.run_root / "manifest.json"
        self._ensure_private_directory(self.run_root)
        self._ensure_private_directory(self.bodies_root)

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)

    def write_body(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        """Durably publish an immutable body and append its pointer to the manifest."""
        safe = "abcdefghijklmnopqrstuvwxyz0123456789-_"
        if not kind or any(character not in safe for character in kind):
            raise ValueError("artifact kind must be a non-empty safe identifier")
        payload = _canonical_json(body)
        digest = hashlib.sha256(payload).hexdigest()
        relative_path = Path("bodies") / f"{kind}-{digest}.json"
        body_path = self.run_root / relative_path

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{kind}-{digest}-",
            suffix=".tmp",
            dir=self.bodies_root,
        )
        temporary_path = Path(temporary_name)
        published = False
        try:
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError(f"failed to write Ask artifact {body_path}")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                os.link(temporary_path, body_path, follow_symlinks=False)
                published = True
            except FileExistsError:
                if body_path.read_bytes() != payload:
                    raise RuntimeError(f"immutable artifact collision at {body_path}") from None
            os.chmod(body_path, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)
        if published:
            directory = os.open(self.bodies_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

        pointer: dict[str, Any] = {
            "kind": kind,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "sha256": digest,
            "relative_path": relative_path.as_posix(),
            "size_bytes": len(payload),
        }
        with exclusive_file_lock(self.manifest_path):
            manifest = self._read_manifest_unlocked()
            known = {item["relative_path"] for item in manifest["artifacts"]}
            if pointer["relative_path"] not in known:
                manifest["artifacts"].append(pointer)
                durable_replace(self.manifest_path, _canonical_json(manifest), mode=0o600)
        return pointer

    def read_body(self, pointer: dict[str, Any]) -> dict[str, Any]:
        if pointer.get("project_id") != self.project_id or pointer.get("run_id") != self.run_id:
            raise ValueError("Ask artifact pointer does not belong to this project and run")
        relative = pointer.get("relative_path")
        expected_hash = pointer.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise ValueError("invalid Ask artifact pointer")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("Ask artifact pointer escapes the run root")
        body_path = (self.run_root / relative_path).resolve()
        if not body_path.is_relative_to(self.run_root.resolve()):
            raise ValueError("Ask artifact pointer escapes the run root")
        payload = body_path.read_bytes()
        actual_hash = hashlib.sha256(payload).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(f"Ask artifact hash mismatch for {relative}")
        if pointer.get("size_bytes") != len(payload):
            raise RuntimeError(f"Ask artifact size mismatch for {relative}")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise RuntimeError(f"Ask artifact body must be an object: {relative}")
        return value

    def verify_manifest(self) -> None:
        """Verify every immutable body currently referenced by the manifest."""
        for pointer in self._read_manifest_unlocked()["artifacts"]:
            self.read_body(pointer)

    def _read_manifest_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads(self.manifest_path.read_bytes())
        except FileNotFoundError:
            return {"schema_version": 1, "artifacts": []}
        if not isinstance(value, dict) or not isinstance(value.get("artifacts"), list):
            raise RuntimeError(f"invalid Ask artifact manifest at {self.manifest_path}")
        return value
