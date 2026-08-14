"""PipelineLoader: typed load, discovery, validation, and revision-aware cache."""

from __future__ import annotations

import json

import pytest

from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.revisions import bump_definitions_revision
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.pipeline_loader import PipelineLoader

pytestmark = pytest.mark.integration


def _seed_pipeline(
    db: HubDatabase,
    name: str,
    *,
    enabled: bool = True,
    project_id: str | None = None,
    extra: dict[str, object] | None = None,
    tags: list[str] | None = None,
    source: str = "installed",
) -> None:
    body: dict[str, object] = {
        "name": name,
        "type": "pipeline",
        "steps": [{"id": "s1", "exec": "echo hi"}],
    }
    if extra:
        body.update(extra)
    PipelineDefinitionManager(db).create(
        name=name,
        definition_json=body,
        enabled=enabled,
        project_id=project_id,
        tags=tags or ["gobby"],
        source=source,  # type: ignore[arg-type]
    )


def test_pipeline_loader_import_and_public_surface() -> None:
    assert hasattr(PipelineLoader, "load_pipeline")
    assert hasattr(PipelineLoader, "discover_pipelines")
    assert hasattr(PipelineLoader, "validate_pipeline_for_agent")
    assert hasattr(PipelineLoader, "clear_cache")
    assert not hasattr(PipelineLoader, "load_workflow")
    assert not hasattr(PipelineLoader, "register_inline_workflow")


def test_workflow_loader_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        __import__("gobby.workflows.loader")
    with pytest.raises(ModuleNotFoundError):
        __import__("gobby.workflows.loader_discovery")


@pytest.mark.asyncio
async def test_load_and_discover_pipelines(temp_db: HubDatabase) -> None:
    _seed_pipeline(temp_db, "review")
    _seed_pipeline(temp_db, "disabled-pipe", enabled=False)
    loader = PipelineLoader(db=temp_db)
    loaded = await loader.load_pipeline("review")
    assert loaded is not None
    assert loaded.name == "review"
    assert loaded.enabled is True
    discovered = await loader.discover_pipelines()
    names = {item.name for item in discovered}
    assert "review" in names
    assert "disabled-pipe" not in names


@pytest.mark.asyncio
async def test_extends_resolution_and_cycle(temp_db: HubDatabase) -> None:
    _seed_pipeline(
        temp_db,
        "base-pipe",
        extra={"steps": [{"id": "base", "exec": "echo base"}]},
    )
    _seed_pipeline(
        temp_db,
        "child-pipe",
        extra={
            "extends": "base-pipe",
            "steps": [{"id": "child", "exec": "echo child"}],
        },
    )
    loader = PipelineLoader(db=temp_db)
    child = await loader.load_pipeline("child-pipe")
    assert child is not None
    assert [step.id for step in child.steps] == ["base", "child"]

    temp_db.execute(
        "UPDATE pipeline_definitions SET definition_json = %s WHERE name = %s",
        (
            json.dumps(
                {
                    "name": "loop-a",
                    "type": "pipeline",
                    "extends": "loop-b",
                    "steps": [{"id": "a", "exec": "echo a"}],
                }
            ),
            "base-pipe",
        ),
    )
    temp_db.execute(
        """
        INSERT INTO pipeline_definitions (
            id, name, enabled, enabled_pinned, version, definition_json, source
        ) VALUES (
            gen_random_uuid(), 'loop-b', true, false, '1.0', %s, 'installed'
        )
        """,
        (
            json.dumps(
                {
                    "name": "loop-b",
                    "type": "pipeline",
                    "extends": "base-pipe",
                    "steps": [{"id": "b", "exec": "echo b"}],
                }
            ),
        ),
    )
    loader.clear_cache()
    with pytest.raises(ValueError, match="Circular"):
        await loader.load_pipeline("base-pipe")


@pytest.mark.asyncio
async def test_revision_aware_cache_refetches_on_drift(temp_db: HubDatabase) -> None:
    _seed_pipeline(temp_db, "cached-pipe")
    loader = PipelineLoader(db=temp_db)
    first = await loader.load_pipeline("cached-pipe")
    assert first is not None
    temp_db.execute(
        "UPDATE pipeline_definitions SET description = %s WHERE name = %s",
        ("after-drift", "cached-pipe"),
    )
    temp_db.execute(
        """
        UPDATE pipeline_definitions
        SET definition_json = definition_json || %s::jsonb
        WHERE name = %s
        """,
        (json.dumps({"description": "after-drift"}), "cached-pipe"),
    )
    stale = await loader.load_pipeline("cached-pipe")
    assert stale is not None
    assert stale.description != "after-drift"
    bump_definitions_revision("pipelines")
    fresh = await loader.load_pipeline("cached-pipe")
    assert fresh is not None
    assert fresh.description == "after-drift"
