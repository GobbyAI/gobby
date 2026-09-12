"""Exercise the installed annotation package through an isolated Gobby daemon."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.skills.sync import sync_bundled_skills
from gobby.storage.hub.protocol import HubDatabase
from tests.e2e import conftest as e2e_fixtures
from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    MCPTestClient,
    daemon_token,
)

daemon_instance = e2e_fixtures.daemon_instance
e2e_config = e2e_fixtures.e2e_config
e2e_project_dir = e2e_fixtures.e2e_project_dir
mcp_client = e2e_fixtures.mcp_client

pytestmark = pytest.mark.integration
PROJECT = "00000000-0000-0000-0000-000000000e2e"
WORKSPACE = Path(__file__).resolve().parents[2] / "gobby-annotate"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
)


def test_internal_routing_uses_registered_identity() -> None:
    manager = InternalRegistryManager()
    manager.add_registry(InternalToolRegistry("gobby-tasks"))
    assert manager.is_internal("gobby-tasks")
    assert not manager.is_internal("gobby-annotate")
    assert not manager.is_internal(None)


@pytest.fixture
def e2e_pre_daemon_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_db: HubDatabase
) -> None:
    """Install the real npm package where the isolated daemon resolves binaries."""
    sync_bundled_mcp_templates(postgres_db)
    sync_bundled_skills(postgres_db)
    subprocess.run(
        ["npm", "pack", "--workspace", "gobby-annotate", "--pack-destination", str(tmp_path)],
        cwd=WORKSPACE,
        check=True,
        capture_output=True,
        timeout=60,
    )
    prefix = tmp_path / "npm"
    subprocess.run(
        [
            "npm",
            "install",
            "--prefix",
            str(prefix),
            "--ignore-scripts",
            str(tmp_path / "gobby-annotate-0.1.0.tgz"),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    monkeypatch.setenv(
        "PATH", str(prefix / "node_modules" / ".bin") + os.pathsep + os.environ["PATH"]
    )


def write_capture(root: Path) -> tuple[str, str]:
    export_id, annotation_id = str(uuid4()), str(uuid4())
    bounds = {"x": 0, "y": 0, "width": 1, "height": 1}
    manifest = {
        "version": 1,
        "batchId": str(uuid4()),
        "exportId": export_id,
        "title": "Checkout review",
        "exportedAt": "2026-09-11T00:00:00Z",
        "annotations": [
            {
                "id": annotation_id,
                "revision": 1,
                "createdAt": "2026-09-11T00:00:00Z",
                "updatedAt": "2026-09-11T00:00:00Z",
                "comment": "Clarify checkout",
                "page": {"url": "https://example.test/", "title": "Shop"},
                "frame": {"url": "https://example.test/", "path": [], "access": "document"},
                "target": {
                    "kind": "element",
                    "locator": ["#checkout"],
                    "tag": "button",
                    "role": "button",
                    "text": "Buy",
                    "bounds": bounds,
                    "screenshotBounds": bounds,
                },
                "viewport": {
                    "layout": {"width": 1, "height": 1},
                    "visual": {
                        "width": 1,
                        "height": 1,
                        "offsetLeft": 0,
                        "offsetTop": 0,
                        "scale": 1,
                    },
                    "devicePixelRatio": 1,
                    "scrollX": 0,
                    "scrollY": 0,
                },
                "screenshot": {
                    "status": "available",
                    "path": "screenshots/page.png",
                    "width": 1,
                    "height": 1,
                },
            }
        ],
    }
    with zipfile.ZipFile(root / f"{export_id}.zip", "w") as archive:
        archive.writestr("capture.json", json.dumps(manifest))
        archive.writestr("screenshots/page.png", PNG)
    return export_id, annotation_id


def test_managed_annotation_template_proxy_and_restart(
    daemon_instance: DaemonInstance,
    mcp_client: MCPTestClient,
) -> None:
    root = daemon_instance.project_dir / ".gobby" / "annotations"
    root.mkdir()
    headers = {"X-Gobby-Project-Id": PROJECT}
    client = mcp_client.client
    client.headers.update(headers)
    templates = client.get("/api/mcp/templates", headers=headers)
    templates.raise_for_status()
    assert "gobby-annotate" in templates.text
    added = client.post(
        "/api/mcp/servers",
        headers=headers,
        json={
            "name": "gobby-annotate",
            "template": "gobby-annotate",
            "scope": "project",
            "project_id": PROJECT,
            "values": {"capture_root": str(root)},
        },
    )
    added.raise_for_status()
    assert added.json()["success"], added.text
    events = CLIEventSimulator(daemon_instance.http_url, daemon_token(daemon_instance.gobby_home))
    try:
        session = events.register_session(
            str(uuid4()), project_id=PROJECT, cwd=str(daemon_instance.project_dir)
        )
        mcp_client.session_id = session["id"]
    finally:
        events.close()

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        schema = mcp_client.get_tool_schema("gobby-annotate", name)
        assert schema.get("success"), schema
        result = mcp_client.call_tool("gobby-annotate", name, arguments)
        assert result.get("success"), result
        return result

    assert "Checkout review" not in json.dumps(call("list_captures", {}))
    export_id, annotation_id = write_capture(root)
    assert export_id in json.dumps(call("list_captures", {}))
    arguments = {"export_id": export_id, "annotation_id": annotation_id}
    note = json.dumps(call("get_annotation", arguments))
    assert "Clarify checkout" in note
    assert base64.b64encode(PNG).decode() not in note
    assert base64.b64encode(PNG).decode() in json.dumps(call("get_screenshot", arguments))

    imported_skill = mcp_client.call_tool("gobby-skills", "get_skill", {"name": "annotate"})
    assert imported_skill.get("success"), imported_skill
    assert "Import Gobby Annotate" in json.dumps(imported_skill)

    def task_call(name: str, values: dict[str, Any]) -> dict[str, Any]:
        mcp_client.get_tool_schema("gobby-tasks", name)
        response = mcp_client.call_tool("gobby-tasks", name, values)
        assert response.get("success"), response
        result = response["result"]
        assert isinstance(result, dict), result
        assert "error" not in result, result
        return result

    batch_id = str(uuid4())
    identity = f"annotate-note:{batch_id}:{annotation_id}"
    assert task_call("list_tasks", {"label": identity})["count"] == 0
    standalone = task_call(
        "create_task",
        {
            "title": "Clarify checkout",
            "category": "code",
            "implementation_domain": "frontend",
            "claim": False,
            "labels": [identity, "annotate-revision:1"],
            "description": f"Original note: Clarify checkout\nTarget: #checkout\nViewport: 1×1 CSS px\nCapture: {root}, export {export_id}, annotation {annotation_id}",
            "validation_criteria": "Checkout clearly communicates the next action and remains keyboard accessible.",
        },
    )
    # A repeated or interrupted import first discovers the initial creation label.
    existing = task_call("list_tasks", {"label": identity})
    assert existing["count"] == 1
    assert existing["tasks"][0]["id"] == standalone["id"]
    details = task_call("get_task", {"task_id": standalone["id"], "brief": False})
    assert details.get("parent_task_id") is None
    assert not details["state"]["is_claimed"]
    assert not details["state"]["allow_automation"]
    assert "annotate-revision:1" in details["labels"]
    assert "annotate-revision:2" not in details["labels"]

    epic = task_call(
        "create_task",
        {
            "title": "Review checkout batch",
            "category": "planning",
            "task_type": "epic",
            "claim": False,
            "labels": [f"annotate-batch:{batch_id}"],
        },
    )
    children: list[str] = []
    for index in range(2):
        child = task_call(
            "create_task",
            {
                "title": f"Apply checkout note {index + 1}",
                "category": "code",
                "implementation_domain": "frontend",
                "parent_task_id": epic["id"],
                "claim": False,
                "depends_on": children[-1:],
                "labels": [f"annotate-note:{batch_id}:{uuid4()}", "annotate-revision:1"],
                "validation_criteria": "The annotated checkout behavior matches the note and passes its focused UI check.",
            },
        )
        children.append(child["id"])
    assert task_call("list_tasks", {"parent_task_id": epic["id"]})["count"] == 2
    assert task_call("list_tasks", {"label": f"annotate-batch:{batch_id}"})["count"] == 1

    for enabled in (False, True):
        response = client.patch(
            "/api/mcp/servers/gobby-annotate",
            headers=headers,
            json={"enabled": enabled, "project_id": PROJECT},
        )
        response.raise_for_status()
        assert response.json()["success"], response.text
    assert "Clarify checkout" in json.dumps(call("get_annotation", arguments))
    daemon_instance.stop()
    daemon_instance.restart()
    assert export_id in json.dumps(call("list_captures", {}))
    assert base64.b64encode(PNG).decode() in json.dumps(call("get_screenshot", arguments))
    servers = client.get("/api/mcp/servers", headers=headers)
    servers.raise_for_status()
    assert str(root) in servers.text
    assert PROJECT in servers.text
