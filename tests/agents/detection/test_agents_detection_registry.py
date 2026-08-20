"""Database sync and cache-boundary tests for detection manifests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from gobby.agents.detection import registry as registry_module
from gobby.agents.detection.matcher import compile_manifest
from gobby.agents.detection.registry import (
    MAX_STALENESS_SECONDS,
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


@pytest.mark.unit
def test_staleness_validation_ceiling_is_independent_of_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Mock()
    monkeypatch.setattr(registry_module, "DEFAULT_STALENESS_SECONDS", 1.0)

    DetectionManifestRegistry(database, staleness_seconds=MAX_STALENESS_SECONDS)

    with pytest.raises(
        ValueError,
        match=rf"between 0 and {MAX_STALENESS_SECONDS:g} seconds",
    ):
        DetectionManifestRegistry(
            database,
            staleness_seconds=MAX_STALENESS_SECONDS + 1.0,
        )


@pytest.mark.unit
def test_refresh_and_reload_reuse_unchanged_fingerprint() -> None:
    provider_id = "registry-unit"
    database = Mock()
    database.fetchall.return_value = [
        {"provider_id": provider_id, "content": _manifest(provider_id, "ready")}
    ]
    now = [0.0]
    registry = DetectionManifestRegistry(
        database,
        staleness_seconds=MAX_STALENESS_SECONDS,
        clock=lambda: now[0],
    )

    original = registry.for_provider(provider_id)
    now[0] = MAX_STALENESS_SECONDS
    refreshed = registry.for_provider(provider_id)
    reloaded_count = registry.reload()
    reloaded = registry.for_provider(provider_id)

    assert original is not None
    assert refreshed is original
    assert reloaded is original
    assert reloaded_count == 1
    assert database.fetchall.call_count == 3


@pytest.mark.unit
def test_bundled_manifests_cover_supported_providers_and_rule_contract() -> None:
    manifests_path = get_bundled_detection_manifests_path()
    providers = {"agy", "claude", "codex", "droid", "grok", "qwen"}
    required_rule_ids = {
        "trust_prompt",
        "loop_prompt",
        "approval_prompt",
        "pane_provider_error",
        "provider_error",
        "context_full",
        "stop_hook_blocked",
        "active_work",
        "queued_continuation",
        "queued_message",
        "status_bar",
        "idle_prompt",
        "stalled_input",
        "source_shaped",
    }

    loaded = {
        path.stem: compile_manifest(path.read_text(encoding="utf-8"))
        for path in manifests_path.glob("*.toml")
    }

    assert loaded.keys() == providers
    assert all(loaded[provider].manifest.id == provider for provider in providers)
    assert all(loaded[provider].issues == () for provider in providers)
    assert all(
        {rule.id for rule in loaded[provider].manifest.rules} == required_rule_ids
        for provider in providers
    )


@pytest.mark.unit
def test_grok_bundled_manifest_matches_recorded_approval_menu() -> None:
    manifests_path = get_bundled_detection_manifests_path()
    compiled = compile_manifest((manifests_path / "grok.toml").read_text(encoding="utf-8"))
    pane = """\
1 (●) Yes, and don't ask again for anything (always-approve mode)
3 (○) Yes, proceed
4 (○) No, reject (type to add feedback)
"""

    evaluation = compiled.match_rule("approval_prompt", pane)

    assert evaluation.match is not None
    assert evaluation.match.state == "blocked"
    assert evaluation.match.reason == "approval"


@pytest.mark.integration
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
    receipt = temp_db.fetchone(
        "SELECT version, filename FROM schema_migrations WHERE version = %s AND filename = %s",
        (375, "baseline@375"),
    )

    assert receipt == {"version": 375, "filename": "baseline@375"}
    assert {
        "provider_id",
        "version",
        "engine",
        "content",
        "source",
        "created_at",
        "updated_at",
    } <= columns


@pytest.mark.integration
def test_sync_removes_orphaned_bundled_manifest_and_preserves_user_manifest(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    bundled_provider_id = "gemini"
    current_provider_id = "agy"
    user_provider_id = "user-profile"
    provider_ids = (bundled_provider_id, current_provider_id, user_provider_id)

    for provider_id in provider_ids:
        temp_db.execute("DELETE FROM detection_manifests WHERE provider_id = %s", (provider_id,))

    try:
        temp_db.execute(
            """
            INSERT INTO detection_manifests (provider_id, version, engine, content, source)
            VALUES (%s, %s, %s, %s, 'bundled')
            """,
            (bundled_provider_id, "1", 1, _manifest(bundled_provider_id, "stale")),
        )
        save_user_detection_manifest(temp_db, _manifest(user_provider_id, "custom"))
        (tmp_path / f"{current_provider_id}.toml").write_text(
            _manifest(current_provider_id, "current"),
            encoding="utf-8",
        )

        sync_bundled_detection_manifests(temp_db, manifests_path=tmp_path)

        assert (
            temp_db.fetchone(
                "SELECT provider_id FROM detection_manifests WHERE provider_id = %s",
                (bundled_provider_id,),
            )
            is None
        )
        assert temp_db.fetchone(
            "SELECT source FROM detection_manifests WHERE provider_id = %s",
            (user_provider_id,),
        ) == {"source": "user"}
    finally:
        for provider_id in provider_ids:
            temp_db.execute(
                "DELETE FROM detection_manifests WHERE provider_id = %s",
                (provider_id,),
            )


@pytest.mark.integration
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
