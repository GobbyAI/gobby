"""DB-backed registry and bundled synchronization for detection manifests."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TypedDict

from gobby.agents.detection.matcher import CompiledManifest, compile_manifest
from gobby.agents.detection.schema import load_manifest
from gobby.paths import get_install_dir
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

DEFAULT_STALENESS_SECONDS = 30.0
MAX_STALENESS_SECONDS = 30.0


class DetectionManifestSyncResult(TypedDict):
    """Counts and controlled errors from one bundled sync."""

    synced: int
    updated: int
    skipped: int
    errors: list[str]


@dataclass(frozen=True, slots=True)
class _ManifestRow:
    provider_id: str
    content: str


def get_bundled_detection_manifests_path() -> Path:
    """Return the packaged per-provider manifest directory."""

    return get_install_dir() / "shared" / "detection"


def _new_sync_result() -> DetectionManifestSyncResult:
    return {"synced": 0, "updated": 0, "skipped": 0, "errors": []}


def sync_bundled_detection_manifests(
    db: HubDatabase,
    manifests_path: Path | None = None,
) -> DetectionManifestSyncResult:
    """Seed and refresh Gobby-owned manifests while preserving user rows."""

    root = manifests_path or get_bundled_detection_manifests_path()
    result = _new_sync_result()
    if not root.exists():
        result["errors"].append(f"Detection manifest directory does not exist: {root}")
        return result

    manifest_paths = sorted(root.glob("*.toml"))
    bundled_provider_ids = {path.stem for path in manifest_paths}

    with db.transaction():
        for path in manifest_paths:
            try:
                content = path.read_text(encoding="utf-8")
                manifest = load_manifest(content)
                if manifest.id != path.stem:
                    raise ValueError(
                        f"manifest id {manifest.id!r} does not match filename {path.name!r}"
                    )
            except (OSError, UnicodeError, ValueError) as exc:
                result["errors"].append(f"{path.name}: {exc}")
                continue

            row = db.fetchone(
                """
                SELECT version, engine, content, source
                  FROM detection_manifests
                 WHERE provider_id = %s
                """,
                (manifest.id,),
            )
            if row is None:
                db.execute(
                    """
                    INSERT INTO detection_manifests (
                        provider_id, version, engine, content, source
                    ) VALUES (%s, %s, %s, %s, 'bundled')
                    """,
                    (manifest.id, manifest.version, manifest.engine, content),
                )
                result["synced"] += 1
                continue

            if row["source"] == "user":
                result["skipped"] += 1
                continue
            if (
                row["version"] == manifest.version
                and row["engine"] == manifest.engine
                and row["content"] == content
            ):
                result["skipped"] += 1
                continue

            db.execute(
                """
                UPDATE detection_manifests
                   SET version = %s,
                       engine = %s,
                       content = %s,
                       source = 'bundled',
                       updated_at = CURRENT_TIMESTAMP
                 WHERE provider_id = %s
                """,
                (manifest.version, manifest.engine, content, manifest.id),
            )
            result["updated"] += 1

        bundled_rows = db.fetchall(
            "SELECT provider_id FROM detection_manifests WHERE source = 'bundled'"
        )
        for row in bundled_rows:
            provider_id = row["provider_id"]
            if provider_id not in bundled_provider_ids:
                db.execute(
                    "DELETE FROM detection_manifests WHERE provider_id = %s AND source = 'bundled'",
                    (provider_id,),
                )
                logger.debug(
                    "Removed orphaned bundled detection manifest",
                    extra={"provider_id": provider_id},
                )

    return result


def save_user_detection_manifest(db: HubDatabase, content: str) -> str:
    """Validate and persist a user-owned manifest, returning its provider id."""

    manifest = load_manifest(content)
    db.execute(
        """
        INSERT INTO detection_manifests (
            provider_id, version, engine, content, source
        ) VALUES (%s, %s, %s, %s, 'user')
        ON CONFLICT (provider_id) DO UPDATE SET
            version = EXCLUDED.version,
            engine = EXCLUDED.engine,
            content = EXCLUDED.content,
            source = 'user',
            updated_at = CURRENT_TIMESTAMP
        """,
        (manifest.id, manifest.version, manifest.engine, content),
    )
    return manifest.id


class DetectionManifestRegistry:
    """Serve compiled manifests through one fingerprint-based cache boundary."""

    def __init__(
        self,
        db: HubDatabase,
        *,
        staleness_seconds: float = DEFAULT_STALENESS_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0.0 <= staleness_seconds <= MAX_STALENESS_SECONDS:
            raise ValueError(
                f"staleness_seconds must be between 0 and {MAX_STALENESS_SECONDS:g} seconds"
            )
        self._db = db
        self._staleness_seconds = staleness_seconds
        self._clock = clock
        self._compiled: dict[str, CompiledManifest] = {}
        self._fingerprints: dict[str, str] = {}
        self._last_check_at: float | None = None
        self._lock = threading.RLock()

    def for_provider(self, provider_id: str) -> CompiledManifest | None:
        """Return the current compiled manifest for one provider."""

        with self._lock:
            now = self._clock()
            if self._last_check_at is None:
                self._reload_at(now)
            elif now - self._last_check_at >= self._staleness_seconds:
                self._refresh_at(now)
            return self._compiled.get(provider_id)

    def reload(self) -> int:
        """Force an immediate DB reload and return the compiled provider count."""

        with self._lock:
            self._reload_at(self._clock())
            return len(self._compiled)

    def _refresh_at(self, now: float) -> None:
        rows = self._read_rows()
        fingerprints = self._fingerprint_rows(rows)
        if fingerprints == self._fingerprints:
            self._last_check_at = now
            return
        self._install_rows(rows, fingerprints, now)

    def _reload_at(self, now: float) -> None:
        rows = self._read_rows()
        fingerprints = self._fingerprint_rows(rows)
        self._install_rows(rows, fingerprints, now)

    def _read_rows(self) -> list[_ManifestRow]:
        rows = self._db.fetchall(
            "SELECT provider_id, content FROM detection_manifests ORDER BY provider_id"
        )
        return [
            _ManifestRow(provider_id=str(row["provider_id"]), content=str(row["content"]))
            for row in rows
        ]

    @staticmethod
    def _fingerprint_rows(rows: list[_ManifestRow]) -> dict[str, str]:
        return {row.provider_id: sha256(row.content.encode("utf-8")).hexdigest() for row in rows}

    def _install_rows(
        self,
        rows: list[_ManifestRow],
        fingerprints: dict[str, str],
        now: float,
    ) -> None:
        compiled: dict[str, CompiledManifest] = {}
        accepted_fingerprints: dict[str, str] = {}
        for row in rows:
            provider_id = row.provider_id
            content = row.content
            try:
                candidate = compile_manifest(content)
                if candidate.manifest.id != provider_id:
                    raise ValueError(
                        f"row provider {provider_id!r} disagrees with manifest id "
                        f"{candidate.manifest.id!r}"
                    )
            except (UnicodeError, ValueError) as exc:
                logger.warning(
                    "Skipping invalid detection manifest",
                    extra={"provider_id": provider_id, "error": str(exc)},
                )
                continue
            compiled[provider_id] = candidate
            accepted_fingerprints[provider_id] = fingerprints[provider_id]

        self._compiled = compiled
        self._fingerprints = accepted_fingerprints
        self._last_check_at = now
