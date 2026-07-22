"""Database sync and cache-boundary tests for detection manifests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from gobby.agents.detection.matcher import compile_manifest
from gobby.agents.detection.registry import (
    DetectionManifestRegistry,
    get_bundled_detection_manifests_path,
    save_user_detection_manifest,
    sync_bundled_detection_manifests,
)
from gobby.mcp_proxy.tools.workflows._import import reload_cache
from gobby.storage.hub.protocol import HubDatabase


def _manifest(provider_id: str, needle: str) -> str:
    return f'''id = "{provider_id}"
version = "1"
engine = 1

[[rules]]
id = "signal"
state = "blocked"
reason = "approval"
priority = 100
region = "whole_recent"
contains = ["{needle}"]
'''


def test_bundled_manifests_cover_supported_providers() -> None:
    manifests_path = get_bundled_detection_manifests_path()
    providers = {"claude", "codex", "gemini", "qwen", "droid"}

    loaded = {
        path.stem: compile_manifest(path.read_text(encoding="utf-8"))
        for path in manifests_path.glob("*.toml")
    }

    assert providers <= loaded.keys()
    assert all(loaded[provider].manifest.id == provider for provider in providers)
    assert all(loaded[provider].issues == () for provider in providers)


def test_detection_manifest_migration_applies(temp_db: HubDatabase) -> None:
    columns = {
        row["column_name"]
        for row in temp_db.fetchall(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'detection_manifests'
            """
        )
    }
    migration = temp_db.fetchone(
        "SELECT version FROM schema_migrations WHERE version = %s",
        (333,),
    )

    assert migration == {"version": 333}
    assert {
        "provider_id",
        "version",
        "engine",
        "content",
        "source",
        "created_at",
        "updated_at",
    } <= columns


def test_cache_boundary_and_user_ownership(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    provider_id = "registry-test"
    manifest_path = tmp_path / f"{provider_id}.toml"
    manifest_path.write_text(_manifest(provider_id, "alpha"), encoding="utf-8")
    now = [0.0]

    temp_db.execute("DELETE FROM detection_manifests WHERE provider_id = %s", (provider_id,))
    try:
        first_sync = sync_bundled_detection_manifests(temp_db, manifests_path=tmp_path)
        registry = DetectionManifestRegistry(
            temp_db,
            staleness_seconds=30.0,
            clock=lambda: now[0],
        )

        original = registry.for_provider(provider_id)
        assert first_sync == {"synced": 1, "updated": 0, "skipped": 0, "errors": []}
        assert original is not None
        assert original.match("alpha").match is not None

        manifest_path.write_text(_manifest(provider_id, "bundled-drift"), encoding="utf-8")
        drift_sync = sync_bundled_detection_manifests(temp_db, manifests_path=tmp_path)
        bundled_row = temp_db.fetchone(
            "SELECT content, source FROM detection_manifests WHERE provider_id = %s",
            (provider_id,),
        )
        assert drift_sync == {"synced": 0, "updated": 1, "skipped": 0, "errors": []}
        assert bundled_row == {
            "content": _manifest(provider_id, "bundled-drift"),
            "source": "bundled",
        }

        user_content = _manifest(provider_id, "beta")
        assert save_user_detection_manifest(temp_db, user_content) == provider_id

        still_cached = registry.for_provider(provider_id)
        assert still_cached is original

        now[0] = 30.0
        refreshed = registry.for_provider(provider_id)
        assert refreshed is not None
        assert refreshed.fingerprint != original.fingerprint
        assert refreshed.match("beta").match is not None

        manifest_path.write_text(_manifest(provider_id, "gamma"), encoding="utf-8")
        second_sync = sync_bundled_detection_manifests(temp_db, manifests_path=tmp_path)
        stored = temp_db.fetchone(
            "SELECT content, source FROM detection_manifests WHERE provider_id = %s",
            (provider_id,),
        )
        assert second_sync == {"synced": 0, "updated": 0, "skipped": 1, "errors": []}
        assert stored == {"content": user_content, "source": "user"}

        explicit_content = _manifest(provider_id, "delta")
        temp_db.execute(
            "UPDATE detection_manifests SET content = %s WHERE provider_id = %s",
            (explicit_content, provider_id),
        )
        loader = Mock()
        reload_result = reload_cache(loader, detection_registry=registry)
        explicit = registry.for_provider(provider_id)

        loader.clear_cache.assert_called_once_with()
        assert reload_result["detection_manifests_reloaded"] == 1
        assert explicit is not None
        assert explicit.match("delta").match is not None
    finally:
        temp_db.execute("DELETE FROM detection_manifests WHERE provider_id = %s", (provider_id,))
