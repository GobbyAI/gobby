"""Template hash cache reads nested agent step_workflow bodies."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from gobby.storage.definitions._shared import compute_definition_hash
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.template_hashes import TemplateHashCache


def test_load_agents_hashes_nested_step_workflow(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "coder.yaml").write_text(
        "name: coder\n"
        "provider: claude\n"
        "mode: interactive\n"
        "step_workflow:\n"
        "  variables:\n"
        "    goal: ship\n"
        "  steps:\n"
        "    - name: implement\n",
        encoding="utf-8",
    )
    cache = TemplateHashCache()
    cache._load_agents(agents_dir)
    original = cache.get_hash("agent", "coder")
    assert original is not None

    (agents_dir / "coder.yaml").write_text(
        "name: coder\n"
        "provider: claude\n"
        "mode: interactive\n"
        "step_workflow:\n"
        "  variables:\n"
        "    goal: ship\n"
        "  steps:\n"
        "    - name: review\n",
        encoding="utf-8",
    )
    cache._hashes.clear()
    cache._json_cache.clear()
    cache._load_agents(agents_dir)
    assert cache.get_hash("agent", "coder") != original

    body = AgentDefinitionBody.model_validate(
        {
            "name": "coder",
            "provider": "claude",
            "mode": "interactive",
            "step_workflow": {
                "variables": {"goal": "ship"},
                "steps": [{"name": "review"}],
            },
        }
    )
    row = SimpleNamespace(name="coder", kind="agent", definition_json=body.model_dump())
    assert cache.has_drift(row) is False
    drifted = dict(body.model_dump())
    drifted["step_workflow"]["steps"] = [{"name": "implement"}]
    assert cache.has_drift(SimpleNamespace(name="coder", kind="agent", definition_json=drifted))
    assert compute_definition_hash(body.model_dump_json()) == cache.get_hash("agent", "coder")


def test_annotate_rows_keys_by_kind_not_workflow_type() -> None:
    cache = TemplateHashCache()
    cache._hashes[("pipeline", "demo")] = compute_definition_hash('{"name":"demo"}')
    kind_rows: list[dict[str, object]] = [
        {
            "name": "demo",
            "kind": "pipeline",
            "definition_json": '{"name":"other"}',
        }
    ]
    legacy_rows: list[dict[str, object]] = [
        {
            "name": "demo",
            "workflow_type": "pipeline",
            "definition_json": '{"name":"other"}',
        }
    ]
    cache.annotate_rows(kind_rows)
    cache.annotate_rows(legacy_rows)
    assert kind_rows[0]["has_template_update"] is True
    assert legacy_rows[0]["has_template_update"] is False
