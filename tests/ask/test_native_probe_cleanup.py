from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.ask import native_probe_harness as harness
from tests.ask.test_native_probe_harness import (
    _SCHEMA,
    _SCOPED_TEST_DATABASE_URL,
    _TEST_DATABASE_URL,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "failure",
    [
        "export_failed",
        "export_incomplete",
        "live_agent",
        "unknown_liveness",
        "snapshot_failed",
    ],
)
def test_finalizer_retains_owned_state_until_export_and_death_are_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    evidence = runtime_root / "launch-policy.json"
    evidence.write_text("captured policy", encoding="utf-8")
    output_dir = tmp_path / "output"
    destructive_calls: list[str] = []

    def snapshot(*_args: object, **_kwargs: object) -> dict[str, Any]:
        if failure == "snapshot_failed":
            raise RuntimeError("snapshot unavailable")
        agents = []
        if failure in {"live_agent", "unknown_liveness"}:
            agents = [
                {"id": "agent", "pid": 321, "live": True if failure == "live_agent" else None}
            ]
        return {"workers": [], "agents": agents}

    def export(*_args: object, **_kwargs: object) -> dict[str, object]:
        if failure == "export_failed":
            raise RuntimeError("export unavailable")
        return {
            "path": "raw-probe.json",
            "sha256": "a" * 64,
            "complete": failure != "export_incomplete",
        }

    monkeypatch.setattr(harness, "_native_ask_execution_ids", lambda *_args: [])
    monkeypatch.setattr(harness, "_process_snapshot", snapshot)
    monkeypatch.setattr(harness, "_terminate_owned_agent_process", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(harness, "_export_raw", export)
    monkeypatch.setattr(
        harness, "_drop_owned_schema", lambda *_args: destructive_calls.append("schema")
    )
    monkeypatch.setattr(
        harness, "_remove_owned_runtime_root", lambda *_args: destructive_calls.append("runtime")
    )

    errors = harness._finalize_contained_probe(
        base_database_url=_TEST_DATABASE_URL,
        scoped_database_url=_SCOPED_TEST_DATABASE_URL,
        schema_name=_SCHEMA,
        schema_created=True,
        project_id="project",
        output_dir=output_dir,
        runtime_root=runtime_root,
        workers={},
        ask_run_ids=[],
        process_sets={},
        failure=None,
    )

    assert destructive_calls == []
    assert errors
    assert evidence.read_text(encoding="utf-8") == "captured policy"
    cleanup = json.loads((output_dir / "cleanup.json").read_bytes())
    assert cleanup["schema"] == {"status": "retained", "name": _SCHEMA}
    assert cleanup["runtime_root"] == {"status": "retained", "path": str(runtime_root)}
